import os
import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from models import TerminationEvent, ExecutionState, ExecutionResult

class BaseInjector(ABC):

    def __init__(self, target_bin: str, target_src: str, target_func: str):
        self.target_bin = target_bin
        self.target_src = target_src
        self.target_func = target_func
        self.base_dir = Path(__file__).parent.resolve()

    @abstractmethod
    def setup(self) -> dict:
        """Returns a dictionary of environment variables required for the injection."""
        pass

    @property
    @abstractmethod
    def gdb_script(self) -> str:
        """Returns the specific GDB Python script path to execute."""
        pass

    def profile_target(self) -> dict:
        """Runs the profiling script and extracts steps and valid registers."""
        env = os.environ.copy()
        env["TARGET_FUNC"] = self.target_func
        profile_script = self.base_dir / "profile_target.py"
        
        result = subprocess.run(
            ["gdb", "-batch", "-q", "-x", str(profile_script), "--args", self.target_bin],
            capture_output=True, text=True, env=env
        )
        
        profile_data = {}
        for line in result.stdout.splitlines():
            if line.startswith("PROFILED_STEPS="):
                profile_data["MAX_STEPS"] = line.split("=")[1]
            elif line.startswith("PROFILED_REGS="):
                profile_data["TARGET_REGS"] = line.split("=")[1]
                
        return profile_data

    def _main_return_line(self) -> int:
        """Finds the line number of the return statement in the source file."""
        lines = Path(self.target_src).read_text(encoding="utf-8").splitlines()
        for index, line in reversed(list(enumerate(lines, start=1))):
            if "return" in line:
                return index
        raise RuntimeError("main return line not found")

    def run(self, args: list[str] | None = None) -> ExecutionResult:
        """Executes a single injection and extracts the final execution state."""
        args = args or []
        run_env = os.environ.copy()
        run_env.update(self.setup())
        
        return_line = self._main_return_line()
        inferior_out = Path(".inferior_out.txt")
        if inferior_out.exists():
            inferior_out.unlink()

        # Export routing variables so the GDB script knows where to stop and dump state
        run_env["RETURN_LINE"] = str(return_line)
        run_env["SRC_FILE"] = Path(self.target_src).name

        command = [
            "gdb", "--batch", "-q",
            "-x", self.gdb_script,
            "--args", self.target_bin, *args
        ]

        try:
            # Using timeout to catch infinite loops caused by corrupted jumps
            process = subprocess.run(command, capture_output=True, text=True, env=run_env, timeout=5)
            exit_code = process.returncode
            stdout = process.stdout
            stderr = process.stderr
        except subprocess.TimeoutExpired as e:
            exit_code = 124
            stdout = e.stdout.decode() if e.stdout else ""
            stderr = e.stderr.decode() if e.stderr else ""

        # Determine the event type based on GDB output and exit code
        event = TerminationEvent.RETURNED
        combined_out = stdout + stderr
        
        if exit_code == 124:
            event = TerminationEvent.TIMEOUT
        elif "Data corruption detected" in combined_out:
            event = TerminationEvent.ASPIS_DATA
        elif "Control-flow signature mismatch" in combined_out:
            event = TerminationEvent.ASPIS_CF
        elif "Program received signal" in combined_out or "Program terminated with signal" in combined_out:
            event = TerminationEvent.CRASHED

        # Extract the internal state
        state = self._extract_state(stdout, stderr, inferior_out)
        
        return ExecutionResult(event=event, state=state)

    def _extract_state(self, stdout: str, stderr: str, inferior_out: Path) -> ExecutionState:
        """Parses the GDB output to extract variables and registers into a typed state."""
        state = ExecutionState(
            stdout=inferior_out.read_text().strip() if inferior_out.exists() else "",
            stderr=stderr
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
    def __init__(self, target_bin: str, target_src: str, target_func: str, data_only: bool = False):
        super().__init__(target_bin, target_src, target_func)
        self.data_only = data_only

    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "inject_smart_register.py")

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
    def __init__(self, target_bin: str, target_src: str, target_func: str, 
                 payload_ptr: str = "", length_var: str = "", 
                 data_type_size: str = "4", data_only: bool = False):
        super().__init__(target_bin, target_src, target_func)
        self.payload_ptr = payload_ptr
        self.length_var = length_var
        self.data_type_size = data_type_size
        self.data_only = data_only

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
            "DATA_TYPE_SIZE": self.data_type_size,
            "DATA_ONLY_MODE": "1" if self.data_only else "0"
        }


class NormalExecution(BaseInjector):
    """Executes the target natively without injecting a fault, capturing the baseline state."""
    
    @property
    def gdb_script(self) -> str:
        return str(self.base_dir / "extract_baseline_state.py")

    def setup(self) -> dict:
        return {}