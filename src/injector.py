import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from src.config_parser import (
    GlobalInjection,
    MemoryInjection,
    RegisterInjection,
    TargetFunction,
)
from src.models import ExecutionResult, ExecutionState, TerminationEvent
from src.env import Environment

class BaseInjector(ABC):

    @staticmethod
    def create_for_target(
        injection: RegisterInjection | MemoryInjection | GlobalInjection,
        env: Environment,
        target_bin: str | Path,
        target_src: str | Path,
        target_function: TargetFunction,
        debugger: str | None = None,
        timeout_ms: int = 5000,
    ) -> list["BaseInjector"]:
        """Build the injectors required by one configured target function."""
        common_args = (
            env,
            target_bin,
            target_src,
            target_function.name,
        )
        common_kwargs = {
            "debugger": debugger,
            "timeout_ms": timeout_ms,
        }

        if isinstance(injection, RegisterInjection):
            return [
                RegisterInjector(
                    *common_args,
                    data_only=injection.data_only,
                    **common_kwargs,
                )
            ]
        if isinstance(injection, MemoryInjection):
            if not target_function.variables:
                return [MemoryInjector(*common_args, **common_kwargs)]
            return [
                MemoryInjector(
                    *common_args,
                    payload_ptr=variable.name,
                    length_var=str(variable.length) if variable.length else "",
                    **common_kwargs,
                )
                for variable in target_function.variables
            ]
        if isinstance(injection, GlobalInjection):
            return [
                GlobalInjector(
                    *common_args,
                    target_global=injection.target_global,
                    **common_kwargs,
                )
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
        timeout_ms: int = 5000,
    ):
        self.env = env
        self.target_bin = Path(target_bin).resolve()
        self.target_src = Path(target_src).resolve()
        self.target_func = target_func
        self.debugger = debugger
        self.timeout_ms = timeout_ms
        self._selected_debugger: str | None = None
        self.base_dir = Path(__file__).parent.resolve() / "gdb_scripts"
        self._setup_lock = threading.Lock()
        self._setup_cache: dict | None = None

    @abstractmethod
    def setup(self) -> dict:
        """Returns a dictionary of environment variables required for the injection."""
        pass

    @property
    @abstractmethod
    def gdb_script(self) -> str:
        """Returns the specific GDB Python script path to execute."""
        pass

    @property
    @abstractmethod
    def lldb_script(self) -> str:
        """Returns the specific LLDB Python script path to execute."""
        pass

    def _debuggers(self) -> list[str]:
        if self.debugger is not None:
            candidates = [self.debugger]
        elif platform.system() == "Darwin":
            candidates = [self.env.lldb_executable, self.env.gdb_executable]
        else:
            candidates = [self.env.gdb_executable, self.env.lldb_executable]

        available = []
        for debugger in candidates:
            if debugger and shutil.which(str(debugger)) and debugger not in available:
                available.append(str(debugger))
        return available

    def _debugger_flavor(self, debugger: str) -> str:
        name = Path(debugger).name.lower()
        if "gdb" in name:
            return "gdb"
        if "lldb" in name:
            return "lldb"
        raise ValueError(f"Unsupported debugger: {debugger}")

    def _script_for(self, debugger: str) -> str:
        return self.gdb_script if self._debugger_flavor(debugger) == "gdb" else self.lldb_script

    def _debugger_command(
        self,
        debugger: str,
        script: str,
        args: list[str] | None = None,
    ) -> list[str | Path]:
        args = args or []
        if self._debugger_flavor(debugger) == "gdb":
            return [
                debugger, "--batch", "-q", "-x", script,
                "--args", self.target_bin, *args,
            ]
        return [
            debugger, "--batch", "--no-lldbinit",
            "-o", f"command script import {script}",
            "--", self.target_bin, *args,
        ]

    def _ordered_debuggers(self) -> list[str]:
        debuggers = self._debuggers()
        if self._selected_debugger in debuggers:
            debuggers.remove(self._selected_debugger)
            debuggers.insert(0, self._selected_debugger)
        return debuggers

    def _debugger_failed_to_start(self, process: subprocess.CompletedProcess) -> bool:
        output = f"{process.stdout}\n{process.stderr}"
        startup_errors = (
            "Don't know how to run",
            "unable to launch the executable",
            "process exited with status -1",
            "error: process launch failed",
        )
        return process.returncode != 0 or any(error.lower() in output.lower() for error in startup_errors)

    def profile_target(self) -> dict:
        """Runs the profiling script and extracts steps and valid registers."""
        env = os.environ.copy()
        env["TARGET_FUNC"] = self.target_func
        env["INFERIOR_OUT"] = os.devnull
        errors = []
        for debugger in self._ordered_debuggers():
            profile_script = self.base_dir / (
                "profile_target.py"
                if self._debugger_flavor(debugger) == "gdb"
                else "profile_target_lldb.py"
            )
            result = subprocess.run(
                self._debugger_command(debugger, str(profile_script)),
                capture_output=True,
                text=True,
                env=env,
                stdin=subprocess.DEVNULL,
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
                self._selected_debugger = debugger
                return profile_data
            errors.append(f"{debugger}: {result.stderr.strip() or result.stdout.strip()}")

        details = "\n".join(errors) or "Neither gdb nor lldb is installed."
        raise RuntimeError(f"Target profiling failed:\n{details}")

    def run(self, args: list[str] | None = None) -> ExecutionResult:
        """Executes a single injection and extracts the final execution state."""
        args = args or []
        run_env = os.environ.copy()
        
        if self._setup_cache is None:
            with self._setup_lock:
                if self._setup_cache is None:
                    self._setup_cache = self.setup()
        run_env.update(self._setup_cache)
        
        with tempfile.NamedTemporaryFile(delete=False) as temp_out:
            inferior_out = Path(temp_out.name)
            
        run_env["INFERIOR_OUT"] = str(inferior_out)
        run_env["ENTRY_FUNC"] = self.target_func

        debuggers = self._ordered_debuggers()
        if not debuggers:
            inferior_out.unlink(missing_ok=True)
            raise RuntimeError("Neither gdb nor lldb is installed.")

        errors = []
        for index, debugger in enumerate(debuggers):
            command = self._debugger_command(
                debugger, self._script_for(debugger), args
            )
            try:
                # Using timeout to catch infinite loops caused by corrupted jumps
                process = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    env=run_env,
                    timeout=self.timeout_ms / 1000.0,
                    stdin=subprocess.DEVNULL,
                )
                exit_code = process.returncode
                stdout = process.stdout
                stderr = process.stderr
            except subprocess.TimeoutExpired as error:
                exit_code = 124
                stdout = self._decode_timeout_output(error.stdout)
                stderr = self._decode_timeout_output(error.stderr)
                break

            if self._debugger_failed_to_start(process) and index + 1 < len(debuggers):
                errors.append(f"{debugger}: {stderr.strip() or stdout.strip()}")
                inferior_out.write_text("", encoding="utf-8")
                continue
            self._selected_debugger = debugger
            break
        else:
            inferior_out.unlink(missing_ok=True)
            raise RuntimeError("Debugger execution failed:\n" + "\n".join(errors))

        # Determine the event type based on GDB output, inferior output, and exit code
        event = TerminationEvent.RETURNED
        
        try:
            inf_out = inferior_out.read_text(encoding="utf-8", errors="replace")
        except:
            inf_out = ""
            
        combined_out = stdout + stderr + inf_out
        
        if exit_code == 124:
            event = TerminationEvent.TIMEOUT
        elif "Data corruption detected" in combined_out:
            event = TerminationEvent.ASPIS_DATA
        elif "Control-flow signature mismatch" in combined_out:
            event = TerminationEvent.ASPIS_CF
        elif (
            "Program received signal" in combined_out
            or "Program terminated with signal" in combined_out
            or "stop reason = signal" in combined_out
        ):
            event = TerminationEvent.CRASHED
        elif "[FATAL]" in combined_out or "Uncaught Exception" in combined_out:
            event = TerminationEvent.INTERNAL_ERROR

        # Extract the internal state
        state = self._extract_state(stdout, stderr, inferior_out)
        
        if inferior_out.exists():
            inferior_out.unlink()
            
        return ExecutionResult(event=event, state=state)

    def _decode_timeout_output(self, output: str | bytes | None) -> str:
        if output is None:
            return ""
        return output.decode() if isinstance(output, bytes) else output

    def _extract_state(self, stdout: str, stderr: str, inferior_out: Path) -> ExecutionState:
        """Parses the GDB output to extract variables and registers into a typed state."""
        state = ExecutionState(
            stdout=inferior_out.read_text().strip() if inferior_out.exists() else "",
            stderr=stderr,
            debugger_output=stdout
        )

        try:
            state.variables = {
                **self._parse_debug_variables(stdout, "@@ARGS@@", "@@LOCALS@@"),
                **self._parse_debug_variables(stdout, "@@LOCALS@@", "@@REGISTERS@@"),
            }
            state.registers = self._parse_debug_registers(stdout)
        except Exception as e:
            # Fallback if the program crashes or ASPIS intercepts before the return statement
            state.error = f"Failed to extract state (Crash or Guardrail): {str(e)}"

        return state

    def _parse_debug_variables(self, output: str, start: str, end: str) -> dict:
        section = self._section(output, start, end)
        variables = {}
        for line in section.splitlines():
            if " = " in line:
                name, value = line.split(" = ", 1)
                match = re.match(r"^\s*(?:\([^)]*\)\s*)?([A-Za-z_]\w*)$", name)
                if match:
                    variables[match.group(1)] = value.strip()
        return variables

    def _parse_debug_registers(self, output: str) -> dict:
        section = self._section(output, "@@REGISTERS@@", None)
        registers = {}
        for line in section.splitlines():
            match = re.match(r"^\s*(\S+)\s+(?:=\s+)?(\S+)", line)
            if match:
                registers[match.group(1)] = match.group(2)
        return registers

    def _section(self, output: str, start: str, end: str | None = None) -> str:
        if start not in output:
            raise ValueError(f"Snapshot marker {start} not found in output.")
        section = output.split(start, 1)[1]
        if end is not None and end in section:
            section = section.split(end, 1)[0]
        return section.strip()


class RegisterInjector(BaseInjector):
    def __init__(self, env: Environment, target_bin: str | Path, target_src: str | Path, target_func: str = "main", data_only: bool = False, debugger: str | None = None, timeout_ms: int = 5000):
        super().__init__(env, target_bin, target_src, target_func, debugger, timeout_ms)
        self.data_only = data_only

    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "inject_registers.py")

    @property
    def lldb_script(self) -> str:
        return str(self.base_dir / "inject_registers_lldb.py")

    def setup(self) -> dict:
        profile_data = self.profile_target()
        if "MAX_STEPS" not in profile_data or "TARGET_REGS" not in profile_data:
            raise RuntimeError("Profiling failed to extract necessary step limits or registers.")
            
        return {
            "TARGET_FUNC": self.target_func,
            "MAX_STEPS": profile_data["MAX_STEPS"],
            "TARGET_REGS": profile_data["TARGET_REGS"],
            "DATA_ONLY_MODE": "1" if self.data_only else "0"
        }


class MemoryInjector(BaseInjector):
    def __init__(self, env: Environment, target_bin: str | Path, target_src: str | Path, target_func: str = "main", 
                 payload_ptr: str = "", length_var: str = "", debugger: str | None = None, timeout_ms: int = 5000):
        super().__init__(env, target_bin, target_src, target_func, debugger, timeout_ms)
        self.payload_ptr = payload_ptr
        self.length_var = length_var
        self.data_only = bool(payload_ptr)

    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "inject_memory.py")

    @property
    def lldb_script(self) -> str:
        return str(self.base_dir / "inject_memory_lldb.py")

    def setup(self) -> dict:
        profile_data = self.profile_target()
        if "MAX_STEPS" not in profile_data:
            raise RuntimeError("Profiling failed to extract maximum dynamic instructions.")
            
        return {
            "TARGET_FUNC"       : self.target_func,
            "MAX_STEPS"         : profile_data["MAX_STEPS"],
            "PAYLOAD_PTR"       : self.payload_ptr,
            "LENGTH_VAR"        : self.length_var,
            "DATA_ONLY_MODE"    : "1" if self.data_only else "0"
        }


class GlobalInjector(BaseInjector):
    def __init__(self, env: Environment, target_bin: str | Path, target_src: str | Path, target_func: str = "main", 
                 target_global: str = "", debugger: str | None = None, timeout_ms: int = 5000):
        super().__init__(env, target_bin, target_src, target_func, debugger, timeout_ms)
        self.target_global = target_global

    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "inject_globals.py")

    @property
    def lldb_script(self) -> str:
        return str(self.base_dir / "inject_globals_lldb.py")

    def setup(self) -> dict:
        profile_data = self.profile_target()
        if "MAX_STEPS" not in profile_data:
            raise RuntimeError("Profiling failed to extract maximum dynamic instructions.")
            
        return {
            "TARGET_FUNC"       : self.target_func,
            "MAX_STEPS"         : profile_data["MAX_STEPS"],
            "TARGET_GLOBAL"     : self.target_global,
            "TARGET_SRC"        : str(self.target_src.name),
        }


class NormalExecution(BaseInjector):
    """Executes the target natively without injecting a fault, capturing the baseline state."""
    
    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "extract_baseline_state.py")

    @property
    def lldb_script(self) -> str:
        return str(self.base_dir / "extract_baseline_state_lldb.py")

    def setup(self) -> dict:
        return {}
