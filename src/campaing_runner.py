from src.config_parser import Campaign
from pathlib import Path
import subprocess
import re
import shlex

class Oracle:
    src:        str
    exe:        str
    results:    dict
    size:       int

    def __init__(self):
        self.src = ""
        self.exe = ""
        self.results = {}
        self.size = 0

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
        debug_result = subprocess.run(
            [
                "gdb", "--batch", "-q", self.exe,
                "-ex", f"break {self.src}:{return_line}",
                "-ex", f"run {shlex.join(args)}",
                "-ex", "printf \"@@ARGS@@\\n\"",
                "-ex", "info args",
                "-ex", "printf \"@@LOCALS@@\\n\"",
                "-ex", "info locals",
                "-ex", "printf \"@@REGISTERS@@\\n\"",
                "-ex", "info registers",
            ],
            capture_output=True,
            text=True,
        )
        if debug_result.returncode != 0:
            raise RuntimeError(f"Debug snapshot failed:\n{debug_result.stderr}")

        variables = {
            **self._parse_gdb_variables(debug_result.stdout, "@@ARGS@@", "@@LOCALS@@"),
            **self._parse_gdb_variables(debug_result.stdout, "@@LOCALS@@", "@@REGISTERS@@"),
        }
        registers = self._parse_gdb_registers(debug_result.stdout)

        if not variables or not registers:
            raise RuntimeError(f"Debug snapshot did not contain variables and registers:\n{debug_result.stdout}")

        return {
            "variables": variables,
            "registers": registers,
        }

    def _parse_gdb_variables(self, output: str, start: str, end: str):
        section = self._section(output, start, end)
        variables = {}
        for line in section.splitlines():
            if " = " in line:
                name, value = line.split(" = ", 1)
                variables[name.strip()] = value.strip()
        return variables

    def _parse_gdb_registers(self, output: str):
        section = self._section(output, "@@REGISTERS@@", None)
        registers = {}
        for line in section.splitlines():
            match = re.match(r"^(\S+)\s+(\S+)", line)
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

        
