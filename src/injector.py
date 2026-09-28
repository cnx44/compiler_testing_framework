import os
import subprocess
import tempfile
import threading
from pathlib import Path

from src import debug_output
from src.config_parser import (
    GlobalInjection,
    Injection,
    MemoryInjection,
    RegisterInjection,
    TargetFunction,
)
from src.config_parser import DEFAULT_TIMEOUT_MS
from src.models import ExecutionResult, ExecutionState, TerminationEvent

GDB_SCRIPTS_DIR = Path(__file__).parent / "gdb_scripts"

# Messages printed by the default ASPIS handlers, or by custom handlers that
# override them (those may even exit successfully).
DATA_CORRUPTION_MARKERS = ("Data corruption detected", "DATA_CORRUPTION_DETECTED")
CONTROL_FLOW_MARKERS = ("Control-flow signature mismatch", "SIG_MISMATCH_DETECTED")
SIGNAL_MARKERS = ("Program received signal", "Program terminated with signal")


def gdb_command(gdb: str, script: str | Path, binary: str | Path, args: list[str]) -> list[str]:
    # -nx: the user's .gdbinit (e.g. pwndbg, "set print pretty") must not
    # change the output format that is parsed.
    return [gdb, "-nx", "--batch", "-q", "-x", str(script), "--args", str(binary), *args]


def dump_env(function: TargetFunction) -> dict[str, str]:
    """Environment telling the GDB scripts which function and variables to dump."""
    return {
        "TARGET_FUNC": function.name,
        "DUMP_VARS": ",".join(
            variable.name + (f":{variable.length}" if variable.length else "")
            for variable in function.variables
        ),
    }


def create_injectors(
    injection: Injection,
    gdb: str,
    target_bin: str | Path,
    target_src: str | Path,
    function: TargetFunction,
    args: list[str] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> list["Injector"]:
    """Build the injectors required by one configured target function.

    Memory and global injection create one injector per selected variable of
    their kind, or a single untargeted injector when there is none.
    """
    def injector(script, **options):
        return Injector(script, options, gdb, target_bin, function, args or [], timeout_ms)

    if isinstance(injection, RegisterInjection):
        return [injector("inject_registers.py",
                         DATA_ONLY_MODE="1" if injection.data_only else "0")]
    if isinstance(injection, MemoryInjection):
        local_vars = [v for v in function.variables if not v.is_global]
        return [
            injector("inject_memory.py", PAYLOAD_PTR=v.name, LENGTH_VAR=str(v.length or ""))
            for v in local_vars
        ] or [injector("inject_memory.py")]
    if isinstance(injection, GlobalInjection):
        global_vars = [v for v in function.variables if v.is_global]
        return [
            injector("inject_globals.py", TARGET_GLOBAL=v.name) for v in global_vars
        ] or [injector("inject_globals.py", TARGET_SRC=Path(target_src).name)]
    raise TypeError(f"Unsupported injection configuration: {type(injection).__name__}")


class Injector:
    """Run a GDB injection script on the protected binary and classify the outcome."""

    def __init__(
        self,
        script: str,
        options: dict[str, str],
        gdb: str,
        target_bin: str | Path,
        function: TargetFunction,
        args: list[str],
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        self.script = GDB_SCRIPTS_DIR / script
        self.options = options
        self.gdb = gdb
        self.target_bin = Path(target_bin).resolve()
        self.function = function
        self.args = args
        self.timeout_ms = timeout_ms
        # Profiling is expensive: it runs once and is shared by the threads
        # that run injections concurrently.
        self._profile_lock = threading.Lock()
        self._profile: dict[str, str] | None = None

    def _run_gdb(self, script: str | Path, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            gdb_command(self.gdb, script, self.target_bin, self.args),
            capture_output=True,
            text=True,
            errors="replace",
            env={**os.environ, **env},
            timeout=self.timeout_ms / 1000,
            stdin=subprocess.DEVNULL,
        )

    def profile(self) -> dict[str, str]:
        """Return MAX_STEPS and TARGET_REGS measured on a fault-free run."""
        with self._profile_lock:
            if self._profile is None:
                self._profile = self._measure_profile()
            return self._profile

    def _measure_profile(self) -> dict[str, str]:
        try:
            result = self._run_gdb(GDB_SCRIPTS_DIR / "profile_target.py",
                                   {"TARGET_FUNC": self.function.name})
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Target profiling timed out after {self.timeout_ms} ms")
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines()
            if line.startswith(("PROFILED_STEPS=", "PROFILED_REGS="))
        )
        steps = values.get("PROFILED_STEPS", "")
        if result.returncode != 0 or not steps.isdigit():
            details = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Target profiling failed:\n{details}")
        if int(steps) < 1:
            raise RuntimeError(f"'{self.function.name}' has no instruction to inject into.")
        if self.script.name == "inject_registers.py" and not values.get("PROFILED_REGS"):
            raise RuntimeError(f"No register found in '{self.function.name}'.")
        return {"MAX_STEPS": steps, "TARGET_REGS": values.get("PROFILED_REGS", "")}

    def run(self) -> ExecutionResult:
        """Execute a single injection and extract the final execution state."""
        env = {**self.profile(), **self.options, **dump_env(self.function)}
        with tempfile.TemporaryDirectory(prefix="injection-") as directory:
            # The GDB scripts redirect the program's own output into these files.
            out, err = Path(directory, "stdout"), Path(directory, "stderr")
            env.update(INFERIOR_OUT=str(out), INFERIOR_ERR=str(err))
            try:
                process = self._run_gdb(self.script, env)
                timed_out = False
                gdb_stdout, gdb_stderr = process.stdout, process.stderr
            except subprocess.TimeoutExpired as error:
                # The timeout catches infinite loops caused by corrupted jumps.
                timed_out = True
                gdb_stdout, gdb_stderr = _text(error.stdout), _text(error.stderr)
            stdout = _read(out)
            stderr = _read(err)

        debugger_output = gdb_stdout + gdb_stderr
        state = ExecutionState(stdout=stdout.strip(), stderr=stderr.strip(),
                               debugger_output=debugger_output)
        dump = debug_output.parse_dump(gdb_stdout)
        if dump is None:
            # The program crashed, or ASPIS stopped it, before the return.
            state.error = "The target function did not reach its return."
        else:
            state.variables, state.registers = dump

        if timed_out:
            event = TerminationEvent.TIMEOUT
        else:
            event = termination_event(process.returncode, debugger_output + stdout + stderr)
        return ExecutionResult(event=event, state=state)


def termination_event(exit_code: int, output: str) -> TerminationEvent:
    if any(marker in output for marker in DATA_CORRUPTION_MARKERS):
        return TerminationEvent.ASPIS_DATA
    if any(marker in output for marker in CONTROL_FLOW_MARKERS):
        return TerminationEvent.ASPIS_CF
    if any(marker in output for marker in SIGNAL_MARKERS):
        return TerminationEvent.CRASHED
    if exit_code != 0 or "[FATAL]" in output:
        return TerminationEvent.INTERNAL_ERROR
    return TerminationEvent.RETURNED


def _text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
