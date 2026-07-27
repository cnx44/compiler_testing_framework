from src.config_parser import Campaign
from pathlib import Path
import subprocess
import re
import shlex
import platform
import shutil

class Oracle:
    src:        str
    exe:        str
    results:    dict
    size:       int

    def __init__(self, debugger: str | None = None):
        self.src = ""
        self.exe = ""
        self.results = {}
        self.size = 0
        self.debugger = debugger

    def oracle_builder(self, test_path: str, args: list[str] | None = None):
        file_path = Path(test_path)
        self.exe = str(file_path.with_suffix(""))
        self.src = str(file_path)
        args = args or []

        try:
            compile_result = subprocess.run(
                ["clang", "-g", "-O0", self.src, "-o", self.exe],
                capture_output=True,
                text=True,
            )
            if compile_result.returncode != 0:
                raise RuntimeError(f"Compilation failed:\n{compile_result.stderr}")
            
            run_result = subprocess.run(
                [self._exe_command(), *args],
                capture_output=True,
                text=True,
            )
            
            self.results = {
                "stdout": run_result.stdout,
                "stderr": run_result.stderr,
            }
            self.results.update(self._debug_snapshot(args))
            self.size = len(run_result.stdout)
            return self.results
        finally:
            self._cleanup()

    def _debug_snapshot(self, args: list[str]):
        return_line = self._main_return_line()
        errors = []
        for debugger in self._debuggers():
            debug_result = self._run_debugger(debugger, return_line, args)
            if debug_result.returncode != 0:
                errors.append(f"{debugger}: {debug_result.stderr.strip()}")
                continue

            variables = {
                **self._parse_debug_variables(debug_result.stdout, 
                                              "@@ARGS@@", 
                                              "@@LOCALS@@"),
                **self._parse_debug_variables(debug_result.stdout, 
                                              "@@LOCALS@@", 
                                              "@@REGISTERS@@"),
            }
            registers = self._parse_debug_registers(debug_result.stdout)
            if variables and registers:
                return {"variables": variables, "registers": registers}
            errors.append(f"{debugger}: no variables or registers were found")

        details = "\n".join(errors) or "Neither gdb nor lldb is installed."
        raise RuntimeError(f"Debug snapshot failed:\n{details}")

    def _debuggers(self):
        if self.debugger is not None:
            candidates = [self.debugger]
        elif platform.system() == "Darwin":
            # LLDB ships with Xcode and works without GDB's macOS code-signing setup.
            candidates = ["lldb", "gdb"]
        else:
            candidates = ["gdb", "lldb"]
        return [debugger for debugger in candidates if shutil.which(debugger)]

    def _run_debugger(self, debugger: str, return_line: int, args: list[str]):
        if debugger == "gdb":
            command = [
                "gdb", "--batch", "-q", self.exe,
                "-ex", f"break {self.src}:{return_line}",
                "-ex", f"run {shlex.join(args)}",
                "-ex", "printf \"@@ARGS@@\\n\"",
                "-ex", "info args",
                "-ex", "printf \"@@LOCALS@@\\n\"",
                "-ex", "info locals",
                "-ex", "printf \"@@REGISTERS@@\\n\"",
                "-ex", "info registers",
            ]
        elif debugger == "lldb":
            source_name = shlex.quote(Path(self.src).name)
            command = [
                "lldb", "--batch", "--no-lldbinit", self.exe,
                "-o", f"breakpoint set --file {source_name} --line {return_line}",
                "-o", f"run {shlex.join(args)}",
                "-o", "script print('@@ARGS@@')",
                "-o", "frame variable",
                "-o", "script print('@@LOCALS@@')",
                "-o", "script print('@@REGISTERS@@')",
                "-o", "register read",
            ]
        else:
            raise ValueError(f"Unsupported debugger: {debugger}")
        return subprocess.run(command, capture_output=True, text=True)

    def _parse_debug_variables(self, output: str, start: str, end: str):
        section = self._section(output, start, end)
        variables = {}
        for line in section.splitlines():
            if " = " in line:
                name, value = line.split(" = ", 1)
                # GDB prints "name = value"; LLDB prints "(type) name = value".
                match = re.match(r"^\s*(?:\([^)]*\)\s*)?([A-Za-z_]\w*)$", name)
                if match:
                    variables[match.group(1)] = value.strip()
        return variables

    def _parse_debug_registers(self, output: str):
        section = self._section(output, "@@REGISTERS@@", None)
        registers = {}
        for line in section.splitlines():
            # GDB: "rax 0x..."; LLDB: "rax = 0x...".
            match = re.match(r"^\s*(\S+)\s+(?:=\s+)?(\S+)", line)
            if match:
                registers[match.group(1)] = match.group(2)
        return registers

    def _section(self, output: str, start: str, end: str | None):
        section = output.split(start, 1)[1]
        if end is not None:
            section = section.split(end, 1)[0]
        return section.strip()

    def _exe_command(self):
        exe_path = Path(self.exe)
        if exe_path.parent == Path("."):
            return f"./{self.exe}"
        return self.exe

    def _cleanup(self):
        if self.exe:
            Path(self.exe).unlink(missing_ok=True)

    def _main_return_line(self):
        lines = Path(self.src).read_text(encoding="utf-8").splitlines()
        for index, line in reversed(list(enumerate(lines, start=1))):
            if "return" in line:
                return index
        raise RuntimeError("main return line not found")


class CampaignRun:
    campain: Campaign

        
