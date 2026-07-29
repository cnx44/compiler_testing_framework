from abc import ABC, abstractmethod
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import tempfile

from src.config_parser import Campaign, ControlFlowMode, DataProtectionMode
from src.env import Envoirment


class ProgramRunner(ABC):
    """Build, execute, and inspect a program with a configured toolchain."""

    def __init__(self, environment: Envoirment, debugger: str | None = None):
        self.environment = environment
        self.debugger = debugger
        self.src = ""
        self.exe = ""
        self.results: dict = {}
        self.size = 0
        self._build_dir: tempfile.TemporaryDirectory[str] | None = None

    def run(self, programs: list[str], args: list[str] | None = None):
        sources = self._source_files(programs)
        args = args or []
        self.src = str(sources[0])
        self._build_dir = tempfile.TemporaryDirectory(prefix="program-run-")
        self.exe = str(Path(self._build_dir.name) / "program")

        try:
            self._compile(sources)
            run_result = subprocess.run(
                [self._exe_command(), *args],
                capture_output=True,
                text=True,
            )
            self.results = {
                "stdout": run_result.stdout,
                "stderr": run_result.stderr,
                "returncode": run_result.returncode,
            }
            self.results.update(self._debug_snapshot(args))
            self.size = len(run_result.stdout)
            return self.results
        finally:
            self._cleanup()

    @abstractmethod
    def _compile(self, sources: list[Path]):
        """Compile ``sources`` and emit the executable at ``self.exe``."""

    def _source_files(self, programs: list[str]):
        sources = [Path(program) for program in programs if Path(program).suffix in {".c", ".cpp"}]
        if not sources:
            raise ValueError("A run must contain at least one C or C++ source file.")
        missing = [str(source) for source in sources if not source.is_file()]
        if missing:
            raise FileNotFoundError(f"Source file(s) not found: {', '.join(missing)}")
        return sources

    def _debug_snapshot(self, args: list[str]):
        return_line = self._main_return_line()
        errors = []
        for debugger in self._debuggers():
            debug_result = self._run_debugger(debugger, return_line, args)
            if debug_result.returncode != 0:
                errors.append(f"{debugger}: {debug_result.stderr.strip()}")
                continue

            variables = {
                **self._parse_debug_variables(debug_result.stdout, "@@ARGS@@", "@@LOCALS@@"),
                **self._parse_debug_variables(debug_result.stdout, "@@LOCALS@@", "@@REGISTERS@@"),
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
                "-ex", 'printf "@@ARGS@@\\n"',
                "-ex", "info args",
                "-ex", 'printf "@@LOCALS@@\\n"',
                "-ex", "info locals",
                "-ex", 'printf "@@REGISTERS@@\\n"',
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
                match = re.match(r"^\s*(?:\([^)]*\)\s*)?([A-Za-z_]\w*)$", name)
                if match:
                    variables[match.group(1)] = value.strip()
        return variables

    def _parse_debug_registers(self, output: str):
        section = self._section(output, "@@REGISTERS@@", None)
        registers = {}
        for line in section.splitlines():
            match = re.match(r"^\s*(\S+)\s+(?:=\s+)?(\S+)", line)
            if match:
                registers[match.group(1)] = match.group(2)
        return registers

    def _section(self, output: str, start: str, end: str | None):
        if start not in output:
            return ""
        section = output.split(start, 1)[1]
        if end is not None and end in section:
            section = section.split(end, 1)[0]
        return section.strip()

    def _exe_command(self):
        return self.exe

    def _cleanup(self):
        if self._build_dir is not None:
            self._build_dir.cleanup()
            self._build_dir = None

    def _main_return_line(self):
        lines = Path(self.src).read_text(encoding="utf-8").splitlines()
        for index, line in reversed(list(enumerate(lines, start=1))):
            if "return" in line:
                return index
        raise RuntimeError("main return line not found")


class OracleRunner(ProgramRunner):
    """Runs the unprotected reference program with the vanilla compiler."""

    def _compile(self, sources: list[Path]):
        compiler = self.environment.vanilla_compiler
        if not compiler:
            raise RuntimeError("Envoirment.vanilla_compiler is not configured.")
        compile_result = subprocess.run(
            [compiler, "-g", "-O0", *(str(source) for source in sources), "-o", self.exe],
            capture_output=True,
            text=True,
        )
        if compile_result.returncode != 0:
            raise RuntimeError(f"Vanilla compilation failed:\n{compile_result.stderr}")


class AspisRunner(ProgramRunner):
    """Runs the protected program built through the ASPIS toolchain."""

    def __init__(
        self,
        environment: Envoirment,
        data_protection: DataProtectionMode = DataProtectionMode.EDDI,
        control_flow_protection: ControlFlowMode = ControlFlowMode.CFCSS,
        debugger: str | None = None,
    ):
        super().__init__(environment, debugger)
        self.data_protection = data_protection
        self.control_flow_protection = control_flow_protection

    def _compile(self, sources: list[Path]):
        aspis_script = Path(self.environment.aspis_path)
        if not self.environment.aspis_path or not aspis_script.is_file():
            raise RuntimeError("Envoirment.aspis_path must point to an ASPIS script.")

        command = [
            str(aspis_script),
            "-g",
            "-o", Path(self.exe).name,
            "--build-dir", str(Path(self.exe).parent),
        ]
        if self.environment.llvm_path:
            command.extend(["--llvm-bin", self.environment.llvm_path])
        command.extend(self._aspis_mode_args())
        command.extend(str(source) for source in sources)

        compile_result = subprocess.run(command, capture_output=True, text=True)
        if compile_result.returncode != 0:
            details = "\n".join(
                part for part in [compile_result.stdout, compile_result.stderr] if part
            )
            raise RuntimeError(f"ASPIS compilation failed:\n{details}")

    def _aspis_mode_args(self):
        data_args = {
            DataProtectionMode.EDDI: ["--eddi"],
            DataProtectionMode.SEDDI: ["--seddi"],
            DataProtectionMode.FDSC: ["--fdsc"],
            DataProtectionMode.NONE: ["--no-dup"],
        }
        control_args = {
            ControlFlowMode.CFCSS: ["--cfcss"],
            ControlFlowMode.RASM: ["--rasm"],
            ControlFlowMode.INTER_RASM: ["--inter-rasm"],
            ControlFlowMode.NONE: ["--no-cfc"],
        }
        return data_args[self.data_protection] + control_args[self.control_flow_protection]


class CampaignRun:
    campain: Campaign
    log: list[str]

    def __init__(
        self,
        campain: Campaign,
        environment: Envoirment,
        log_path: str = "CampainRun.log",
        debugger: str | None = None,
    ):
        self.campain = campain
        self.environment = environment
        self.log_path = Path(log_path)
        self.debugger = debugger
        self.log = []

    def run_campaing(self, args: list[str] | None = None):
        self.log = []
        for run in self.campain.runs:
            self.log.append(f"RUN NAME: {run.name}")
            entry_source = self._entry_source(run.programs)
            if entry_source is None:
                self.log.append("No C or C++ source file found.")
                self.log.append("")
                continue

            run_args = args if args is not None else self._args_from_values_file(entry_source)
            self.log.append(f"Sources: {', '.join(run.programs)}")
            self.log.append(f"Args: {shlex.join(run_args) if run_args else '(none)'}")

            try:
                # compiling and running with vanilla LLVM install
                oracle_results = OracleRunner(self.environment, self.debugger).run(run.programs, run_args)

                # compiling and runing via ASPIS shell script
                aspis_results = AspisRunner(
                    self.environment,
                    data_protection=run.data_protection,
                    control_flow_protection=run.control_flow_protection,
                    debugger=self.debugger,
                ).run(run.programs, run_args)

                # TODO: Maybe tnis can be logged raw or in some structure and let 
                # the print module to handle this
                self.log.extend(self._comparison_lines(oracle_results, aspis_results, run.vars))
            except Exception as error:
                self.log.append(f"ERROR: {error}")
            self.log.append("")

        self.log_path.write_text("\n".join(self.log), encoding="utf-8")
        return self.log

    def _entry_source(self, programs: list[str]):
        return next((program for program in programs if Path(program).suffix in {".c", ".cpp", ".rs"}), None)

    def _args_from_values_file(self, source_file: str):
        values_path = Path(source_file).with_suffix(".values")
        if not values_path.is_file():
            return []

        args = {}
        for line in values_path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*args\[(\d+)\]\s*=\s*(.+?)\s*$", line)
            if match:
                args[int(match.group(1))] = match.group(2)
        return [args[index] for index in sorted(args)]

    def _comparison_lines(self, oracle_results: dict, aspis_results: dict, vars_to_check: list[str]):
        lines = ["Comparison:"]
        lines.extend(self._field_comparison("returncode", oracle_results, aspis_results))
        lines.extend(self._field_comparison("stdout", oracle_results, aspis_results))
        lines.extend(self._field_comparison("stderr", oracle_results, aspis_results))

        oracle_vars = oracle_results.get("variables", {})
        aspis_vars = aspis_results.get("variables", {})
        variable_names = vars_to_check or sorted(set(oracle_vars) | set(aspis_vars))
        lines.append("variables:")
        for name in variable_names:
            expected = oracle_vars.get(name, "<missing>")
            actual = aspis_vars.get(name, "<missing>")
            status = "OK" if expected == actual else "DIFF"
            lines.append(f"  {status} {name}: oracle={expected} aspis={actual}")
        return lines

    def _field_comparison(self, field: str, oracle_results: dict, aspis_results: dict):
        expected = oracle_results.get(field, "")
        actual = aspis_results.get(field, "")
        status = "OK" if expected == actual else "DIFF"
        return [
            f"{field}: {status}",
            f"  oracle: {expected!r}",
            f"  aspis: {actual!r}",
        ]
