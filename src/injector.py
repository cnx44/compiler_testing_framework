import os
import shutil
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from src import debug_output
from src.constants import DEFAULT_TIMEOUT_MS
from src.config_parser import (
    GlobalInjection,
    MemoryInjection,
    RegisterInjection,
    TargetFunction,
)
from src.models import ExecutionResult, ExecutionState, TerminationEvent
from src.env import Environment

# Exit code reported for a debugger run killed by our timeout (as timeout(1) does).
TIMEOUT_EXIT_CODE = 124

# Messages printed by the default ASPIS handlers, or by custom handlers that
# override them (those may even exit successfully).
DATA_CORRUPTION_MARKERS = ("Data corruption detected", "DATA_CORRUPTION_DETECTED")
CONTROL_FLOW_MARKERS = ("Control-flow signature mismatch", "SIG_MISMATCH_DETECTED")
SIGNAL_MARKERS = (
    "Program received signal",
    "Program terminated with signal",
    "stop reason = signal",
)


class BaseInjector(ABC):

    @staticmethod
    def create_for_target(
        injection: RegisterInjection | MemoryInjection | GlobalInjection,
        env: Environment,
        target_bin: str | Path,
        target_src: str | Path,
        target_function: TargetFunction,
        debugger: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> list["BaseInjector"]:
        """Build the injectors required by one configured target function.

        Memory and global injection create one injector per selected variable,
        or a single untargeted injector when no variable of that kind is set.
        """
        common_args = (env, target_bin, target_src, target_function.name)
        common_kwargs = {"debugger": debugger, "timeout_ms": timeout_ms}
        lengths = {v.name: v.length for v in target_function.variables if v.length}
        if lengths:
            common_kwargs["var_lengths"] = ",".join(
                f"{name}:{length}" for name, length in lengths.items()
            )

        if isinstance(injection, RegisterInjection):
            return [
                RegisterInjector(
                    *common_args, data_only=injection.data_only, **common_kwargs
                )
            ]
        if isinstance(injection, MemoryInjection):
            local_vars = [v for v in target_function.variables if not v.is_global]
            if not local_vars:
                return [MemoryInjector(*common_args, **common_kwargs)]
            return [
                MemoryInjector(
                    *common_args,
                    payload_ptr=variable.name,
                    length_var=str(variable.length) if variable.length else "",
                    **common_kwargs,
                )
                for variable in local_vars
            ]
        if isinstance(injection, GlobalInjection):
            global_vars = [v for v in target_function.variables if v.is_global]
            if not global_vars:
                return [GlobalInjector(*common_args, **common_kwargs)]
            return [
                GlobalInjector(*common_args, target_global=variable.name, **common_kwargs)
                for variable in global_vars
            ]
        raise TypeError(
            f"Unsupported injection configuration: {type(injection).__name__}"
        )

    def __init__(
        self,
        env: Environment,
        target_bin: str | Path,
        target_src: str | Path,
        target_func: str = "main",
        debugger: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        var_lengths: str = "",
    ):
        self.env = env
        self.target_bin = Path(target_bin).resolve()
        self.target_src = Path(target_src).resolve()
        self.target_func = target_func
        self.debugger = debugger
        self.timeout_ms = timeout_ms
        self.var_lengths = var_lengths
        self.base_dir = Path(__file__).parent.resolve() / "gdb_scripts"
        # Profiling is expensive, so setup() runs once per argument list and is
        # shared by the threads that run injections concurrently.
        self._setup_lock = threading.Lock()
        self._setup_cache: dict | None = None
        self._setup_args: tuple[str, ...] | None = None
        self._profile_args: list[str] = []

    @abstractmethod
    def setup(self) -> dict:
        """Returns a dictionary of environment variables required for the injection."""

    @property
    @abstractmethod
    def gdb_script(self) -> str:
        """Returns the specific GDB Python script path to execute."""

    def _debugger(self) -> str | None:
        """Return the configured debugger if it can be found, otherwise None."""
        debugger = self.debugger if self.debugger is not None else self.env.gdb_executable
        if debugger and shutil.which(str(debugger)):
            return str(debugger)
        return None

    def _debugger_command(
        self,
        debugger: str,
        script: str,
        args: list[str] | None = None,
    ) -> list[str | Path]:
        return [
            debugger, "--batch", "-q", "-x", script,
            "--args", self.target_bin, *(args or []),
        ]

    def _debugger_failed_to_start(self, process: subprocess.CompletedProcess) -> bool:
        output = f"{process.stdout}\n{process.stderr}".lower()
        startup_errors = (
            "don't know how to run",
            "unable to launch the executable",
            "process exited with status -1",
            "error: process launch failed",
        )
        return process.returncode != 0 or any(error in output for error in startup_errors)

    def profile_target(self) -> dict:
        """Runs the profiling script and extracts steps and valid registers."""
        debugger = self._debugger()
        if debugger is None:
            raise RuntimeError("Target profiling failed:\ngdb is not installed.")

        env = os.environ.copy()
        env["TARGET_FUNC"] = self.target_func
        env["INFERIOR_OUT"] = os.devnull
        profile_script = str(self.base_dir / "profile_target.py")
        try:
            result = subprocess.run(
                self._debugger_command(debugger, profile_script, self._profile_args),
                capture_output=True,
                text=True,
                env=env,
                timeout=self.timeout_ms / 1000.0,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "Target profiling failed:\n"
                f"{debugger}: profiling timed out after {self.timeout_ms} ms"
            )

        profile_data = {}
        for line in result.stdout.splitlines():
            if line.startswith("PROFILED_STEPS="):
                profile_data["MAX_STEPS"] = line.split("=", 1)[1]
            elif line.startswith("PROFILED_REGS="):
                profile_data["TARGET_REGS"] = line.split("=", 1)[1]

        if (
            not self._debugger_failed_to_start(result)
            and profile_data.get("MAX_STEPS", "").isdigit()
        ):
            return profile_data
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Target profiling failed:\n{debugger}: {details}")

    def run(self, args: list[str] | None = None) -> ExecutionResult:
        """Executes a single injection and extracts the final execution state."""
        args = args or []
        run_env = os.environ.copy()

        with self._setup_lock:
            if self._setup_cache is None or self._setup_args != tuple(args):
                self._profile_args = list(args)
                self._setup_cache = self.setup()
                self._setup_args = tuple(args)
            run_env.update(self._setup_cache)

        debugger = self._debugger()
        if debugger is None:
            raise RuntimeError("gdb is not installed.")

        # The GDB scripts redirect the program's own stdout into this file.
        with tempfile.NamedTemporaryFile(delete=False) as temp_out:
            inferior_out = Path(temp_out.name)
        run_env["INFERIOR_OUT"] = str(inferior_out)
        run_env["ENTRY_FUNC"] = self.target_func
        if self.var_lengths:
            run_env["VAR_LENGTHS"] = self.var_lengths

        try:
            # The timeout catches infinite loops caused by corrupted jumps.
            try:
                process = subprocess.run(
                    self._debugger_command(debugger, self.gdb_script, args),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=run_env,
                    timeout=self.timeout_ms / 1000.0,
                    stdin=subprocess.DEVNULL,
                )
                exit_code, stdout, stderr = process.returncode, process.stdout, process.stderr
            except subprocess.TimeoutExpired as error:
                exit_code = TIMEOUT_EXIT_CODE
                stdout = self._decode_timeout_output(error.stdout)
                stderr = self._decode_timeout_output(error.stderr)

            try:
                program_output = inferior_out.read_text(encoding="utf-8", errors="replace")
            except OSError:
                program_output = ""

            event = self._termination_event(exit_code, stdout + stderr + program_output)
            state = self._extract_state(stdout, stderr, inferior_out)
        finally:
            inferior_out.unlink(missing_ok=True)

        return ExecutionResult(event=event, state=state)

    @staticmethod
    def _termination_event(exit_code: int, output: str) -> TerminationEvent:
        if exit_code == TIMEOUT_EXIT_CODE:
            return TerminationEvent.TIMEOUT
        if any(marker in output for marker in DATA_CORRUPTION_MARKERS):
            return TerminationEvent.ASPIS_DATA
        if any(marker in output for marker in CONTROL_FLOW_MARKERS):
            return TerminationEvent.ASPIS_CF
        if any(marker in output for marker in SIGNAL_MARKERS):
            return TerminationEvent.CRASHED
        if exit_code != 0 or "[FATAL]" in output or "Uncaught Exception" in output:
            return TerminationEvent.INTERNAL_ERROR
        return TerminationEvent.RETURNED

    def _decode_timeout_output(self, output: str | bytes | None) -> str:
        if output is None:
            return ""
        return output.decode() if isinstance(output, bytes) else output

    def _extract_state(self, stdout: str, stderr: str, inferior_out: Path) -> ExecutionState:
        """Parses the GDB output to extract variables and registers into a typed state."""
        state = ExecutionState(
            stdout=(
                inferior_out.read_text(encoding="utf-8", errors="replace").strip()
                if inferior_out.exists()
                else ""
            ),
            stderr=stderr,
            debugger_output=stdout,
        )

        try:
            state.variables = {
                **self._parse_debug_variables(stdout, "@@ARGS@@", "@@LOCALS@@"),
                **self._parse_debug_variables(stdout, "@@LOCALS@@", "@@REGISTERS@@"),
            }
            state.registers = debug_output.parse_registers(
                self._section(stdout, "@@REGISTERS@@")
            )
        except Exception as e:
            # The program crashed, or ASPIS stopped it, before the return statement.
            state.error = f"Failed to extract state (Crash or Guardrail): {str(e)}"

        return state

    def _parse_debug_variables(self, output: str, start: str, end: str) -> dict:
        return debug_output.parse_variables(self._section(output, start, end))

    def _section(self, output: str, start: str, end: str | None = None) -> str:
        text = debug_output.section(output, start, end)
        if text is None:
            raise ValueError(f"Snapshot marker {start} not found in output.")
        return text


class RegisterInjector(BaseInjector):
    def __init__(
        self,
        env: Environment,
        target_bin: str | Path,
        target_src: str | Path,
        target_func: str = "main",
        data_only: bool = False,
        debugger: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        var_lengths: str = "",
    ):
        super().__init__(env, target_bin, target_src, target_func, debugger, timeout_ms, var_lengths)
        self.data_only = data_only

    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "inject_registers.py")

    def setup(self) -> dict:
        profile_data = self.profile_target()
        if "MAX_STEPS" not in profile_data or "TARGET_REGS" not in profile_data:
            raise RuntimeError("Profiling failed to extract necessary step limits or registers.")
        return {
            "TARGET_FUNC": self.target_func,
            "MAX_STEPS": profile_data["MAX_STEPS"],
            "TARGET_REGS": profile_data["TARGET_REGS"],
            "DATA_ONLY_MODE": "1" if self.data_only else "0",
        }


class MemoryInjector(BaseInjector):
    def __init__(
        self,
        env: Environment,
        target_bin: str | Path,
        target_src: str | Path,
        target_func: str = "main",
        payload_ptr: str = "",
        length_var: str = "",
        debugger: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        var_lengths: str = "",
    ):
        super().__init__(env, target_bin, target_src, target_func, debugger, timeout_ms, var_lengths)
        self.payload_ptr = payload_ptr
        self.length_var = length_var
        # With a named variable the fault lands inside it; otherwise on the stack.
        self.data_only = bool(payload_ptr)

    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "inject_memory.py")

    def setup(self) -> dict:
        profile_data = self.profile_target()
        if "MAX_STEPS" not in profile_data:
            raise RuntimeError("Profiling failed to extract maximum dynamic instructions.")
        return {
            "TARGET_FUNC": self.target_func,
            "MAX_STEPS": profile_data["MAX_STEPS"],
            "PAYLOAD_PTR": self.payload_ptr,
            "LENGTH_VAR": self.length_var,
            "DATA_ONLY_MODE": "1" if self.data_only else "0",
        }


class GlobalInjector(BaseInjector):
    def __init__(
        self,
        env: Environment,
        target_bin: str | Path,
        target_src: str | Path,
        target_func: str = "main",
        target_global: str = "",
        debugger: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        var_lengths: str = "",
    ):
        super().__init__(env, target_bin, target_src, target_func, debugger, timeout_ms, var_lengths)
        self.target_global = target_global

    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "inject_globals.py")

    def setup(self) -> dict:
        profile_data = self.profile_target()
        if "MAX_STEPS" not in profile_data:
            raise RuntimeError("Profiling failed to extract maximum dynamic instructions.")
        return {
            "TARGET_FUNC": self.target_func,
            "MAX_STEPS": profile_data["MAX_STEPS"],
            "TARGET_GLOBAL": self.target_global,
            "TARGET_SRC": str(self.target_src.name),
        }


class NormalExecution(BaseInjector):
    """Executes the target natively without injecting a fault, capturing the baseline state."""

    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "extract_baseline_state.py")

    def setup(self) -> dict:
        return {}
