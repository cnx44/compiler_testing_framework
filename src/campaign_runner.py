from abc import ABC, abstractmethod
import math
from pathlib import Path
import platform
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time

from src.config_parser import Campaign, ControlFlowMode, DataProtectionMode
from src.env import Environment


class ProgramRunner(ABC):
    """Build, execute, and inspect a program with a configured toolchain."""

    def __init__(self, environment: Environment, debugger: str | None = None):
        self.environment = environment
        self.debugger = debugger
        self.src = ""
        self.exe = ""
        self.results: dict = {}
        self.size = 0
        self._build_dir: tempfile.TemporaryDirectory[str] | None = None

    def compile(
        self,
        programs: list[str],
        compiler_flags: list[str] | None = None,
    ):
        """Compile ``programs`` and keep the resulting binary available."""
        sources = self._source_files(programs)
        compiler_flags = compiler_flags or []
        self.src = str(sources[0])
        self._cleanup()
        self._build_dir = tempfile.TemporaryDirectory(prefix="program-run-")
        self.exe = str(Path(self._build_dir.name) / "program")

        try:
            self._compile(sources, compiler_flags)
            executable = Path(self.exe)
            if not executable.is_file():
                raise RuntimeError(
                    f"Compilation did not emit the expected binary: {self.exe}"
                )
            self.size = executable.stat().st_size
            return {
                "binary_path": self.exe,
                "binary_size_bytes": self.size,
            }
        except Exception:
            self._cleanup()
            raise

    def execute(
        self,
        args: list[str] | None = None,
        variables_to_check: list[str] | None = None,
        execution_repetitions: int = 1,
    ):
        """Execute an already compiled binary and collect its observable state."""
        if self._build_dir is None or not Path(self.exe).is_file():
            raise RuntimeError("The program must be compiled before it can be executed.")
        if not isinstance(execution_repetitions, int) or execution_repetitions < 1:
            raise ValueError("execution_repetitions must be a positive integer.")

        args = args or []
        execution_times = []
        run_result = None
        for _ in range(execution_repetitions):
            started_at = time.perf_counter()
            run_result = subprocess.run(
                [self._exe_command(), *args],
                capture_output=True,
                text=True,
            )
            execution_times.append(time.perf_counter() - started_at)

        mean = statistics.fmean(execution_times)
        standard_deviation = (
            statistics.stdev(execution_times)
            if execution_repetitions > 1
            else 0.0
        )
        self.results = {
            "stdout": run_result.stdout,
            "stderr": run_result.stderr,
            "returncode": run_result.returncode,
            "execution_time_seconds": mean,
            "execution_time_samples_seconds": execution_times,
            "execution_time_stddev_seconds": standard_deviation,
            "execution_time_standard_error_seconds": (
                standard_deviation / math.sqrt(execution_repetitions)
            ),
            "execution_repetitions": execution_repetitions,
            "binary_size_bytes": self.size,
        }
        self.results.update(self._debug_snapshot(args, variables_to_check))
        return self.results

    def run(
        self,
        programs: list[str],
        args: list[str] | None = None,
        compiler_flags: list[str] | None = None,
        variables_to_check: list[str] | None = None,
        execution_repetitions: int = 1,
    ):
        """Compile and execute a program, cleaning temporary artifacts afterwards."""
        try:
            self.compile(programs, compiler_flags)
            return self.execute(
                args, variables_to_check, execution_repetitions
            )
        finally:
            self.cleanup()

    def cleanup(self):
        """Release the temporary build directory, if present."""
        self._cleanup()

    @abstractmethod
    def _compile(self, sources: list[Path], compiler_flags: list[str]):
        """Compile ``sources`` and emit the executable at ``self.exe``."""

    def _source_files(self, programs: list[str]):
        sources = [Path(program) for program in programs if Path(program).suffix in {".c", ".cpp"}]
        if not sources:
            raise ValueError("A run must contain at least one C or C++ source file.")
        missing = [str(source) for source in sources if not source.is_file()]
        if missing:
            raise FileNotFoundError(f"Source file(s) not found: {', '.join(missing)}")
        return sources

    def _debug_snapshot(
        self,
        args: list[str],
        variables_to_check: list[str] | None = None,
    ):
        return_line = self._main_return_line()
        errors = []
        for debugger in self._debuggers():
            debug_result = self._run_debugger(
                debugger, return_line, args, variables_to_check
            )
            if debug_result.returncode != 0:
                errors.append(f"{debugger}: {debug_result.stderr.strip()}")
                continue

            locals_end = (
                "@@GLOBALS@@"
                if "@@GLOBALS@@" in debug_result.stdout
                else "@@REGISTERS@@"
            )
            variables = {
                **self._parse_debug_variables(
                    debug_result.stdout, "@@GLOBALS@@", "@@REGISTERS@@"
                ),
                **self._parse_debug_variables(
                    debug_result.stdout, "@@ARGS@@", "@@LOCALS@@"
                ),
                **self._parse_debug_variables(
                    debug_result.stdout, "@@LOCALS@@", locals_end
                ),
            }
            registers = self._parse_debug_registers(debug_result.stdout)
            if registers:
                return {"variables": variables, "registers": registers}
            errors.append(f"{debugger}: no registers were found")

        details = "\n".join(errors) or "Neither gdb nor lldb is installed."
        raise RuntimeError(f"Debug snapshot failed:\n{details}")

    def _debuggers(self):
        if self.debugger is not None:
            candidates = [self.debugger]
        elif platform.system() == "Darwin":
            candidates = [self.environment.lldb_executable, self.environment.gdb_executable]
        else:
            candidates = [self.environment.gdb_executable, self.environment.lldb_executable]
        return [debugger for debugger in candidates if shutil.which(debugger)]

    def _run_debugger(
        self,
        debugger: str,
        return_line: int,
        args: list[str],
        variables_to_check: list[str] | None = None,
    ):
        variable_names = self._debug_variable_names(variables_to_check or [])
        debugger_name = Path(debugger).name.lower()
        if "gdb" in debugger_name:
            flavor = "gdb"
        elif "lldb" in debugger_name:
            flavor = "lldb"
        else:
            raise ValueError(f"Unsupported debugger: {debugger}")
        
        if flavor == "gdb":
            command = [
                debugger, "--batch", "-q", self.exe,
                "-ex", f"break {self.src}:{return_line}",
                "-ex", f"run {shlex.join(args)}",
                "-ex", 'printf "@@ARGS@@\\n"',
                "-ex", "info args",
                "-ex", 'printf "@@LOCALS@@\\n"',
                "-ex", "info locals",
                "-ex", 'printf "@@GLOBALS@@\\n"',
            ]
            for name in variable_names:
                command.extend([
                    "-ex", f'printf "{name} = "',
                    "-ex", f"output {name}",
                    "-ex", 'printf "\\n"',
                ])
            command.extend([
                "-ex", 'printf "@@REGISTERS@@\\n"',
                "-ex", "info registers",
            ])
        elif flavor == "lldb":
            source_name = shlex.quote(Path(self.src).name)
            command = [
                debugger, "--batch", "--no-lldbinit", self.exe,
                "-o", f"breakpoint set --file {source_name} --line {return_line}",
                "-o", f"run {shlex.join(args)}",
                "-o", "script print('@@ARGS@@')",
                "-o", "frame variable",
                "-o", "script print('@@LOCALS@@')",
                "-o", "script print('@@GLOBALS@@')",
            ]
            for name in variable_names:
                command.extend(["-o", f"target variable {name}"])
            command.extend([
                "-o", "script print('@@REGISTERS@@')",
                "-o", "register read",
            ])
        else:
            raise ValueError(f"Unsupported debugger: {debugger}")
        return subprocess.run(command, capture_output=True, text=True)

    def _debug_variable_names(self, variable_names: list[str]):
        invalid = [
            name
            for name in variable_names
            if re.fullmatch(r"[A-Za-z_]\w*", name) is None
        ]
        if invalid:
            raise ValueError(
                f"Invalid debugger variable name(s): {', '.join(invalid)}"
            )
        return variable_names

    def _parse_debug_variables(self, output: str, start: str, end: str):
        section = self._section(output, start, end)
        variables = {}
        for line in section.splitlines():
            if " = " in line:
                name, value = line.split(" = ", 1)
                match = re.match(r"^\s*(?:\([^)]*\)\s*)?([A-Za-z_]\w*)$", name)
                if match and value.strip():
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
        gdb_scripts_dir = str(Path(__file__).parent.resolve() / "gdb_scripts")
        script = (
            f"python import sys; sys.path.append('{gdb_scripts_dir}'); "
            "import gdb_utils; "
            "print('MAX_LINE=' + str(gdb_utils.get_end_line('main')))"
        )
        result = subprocess.run(
            [self.environment.gdb_executable, "--batch", "-q", self.exe, "-ex", script],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("MAX_LINE="):
                max_line = int(line.split("=", 1)[1])
                if max_line != -1:
                    return max_line
                break
        raise RuntimeError(f"Could not determine main return line via GDB DWARF symbols.\n{result.stderr}")


class OracleRunner(ProgramRunner):
    """Runs the unprotected reference program with the vanilla compiler."""

    def _compile(
        self,
        sources: list[Path],
        compiler_flags: list[str] | None = None,
    ):
        compiler = self.environment.vanilla_compiler
        if not compiler:
            raise RuntimeError("Environment.vanilla_compiler is not configured.")
        compile_result = subprocess.run(
            [
                compiler,
                "-g",
                "-O0",
                *(str(source) for source in sources),
                *(compiler_flags or []),
                "-o",
                self.exe,
            ],
            capture_output=True,
            text=True,
        )
        if compile_result.returncode != 0:
            raise RuntimeError(f"Vanilla compilation failed:\n{compile_result.stderr}")


class AspisRunner(ProgramRunner):
    """Runs the protected program built through the ASPIS toolchain."""

    def __init__(
        self,
        environment: Environment,
        data_protection: DataProtectionMode = DataProtectionMode.EDDI,
        control_flow_protection: ControlFlowMode = ControlFlowMode.CFCSS,
        debugger: str | None = None,
    ):
        super().__init__(environment, debugger)
        self.data_protection = data_protection
        self.control_flow_protection = control_flow_protection

    def _compile(
        self,
        sources: list[Path],
        compiler_flags: list[str] | None = None,
    ):
        aspis_script = Path(self.environment.aspis_path).resolve()
        if not self.environment.aspis_path or not aspis_script.is_file():
            raise RuntimeError("Environment.aspis_path must point to an ASPIS script.")

        build_dir = Path(self.exe).parent
        command = [
            str(aspis_script),
            "-g",
            "-o", Path(self.exe).name,
            "--build-dir", str(build_dir),
        ]
        if self.environment.llvm_path:
            command.extend(["--llvm-bin", self.environment.llvm_path])
        command.extend(self._aspis_mode_args())
        command.extend(str(source.resolve()) for source in sources)
        command.extend(self._compiler_flags_for_build_dir(compiler_flags or []))

        compile_result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=build_dir,
        )
        if compile_result.returncode != 0:
            details = "\n".join(
                part for part in [compile_result.stdout, compile_result.stderr] if part
            )
            raise RuntimeError(f"ASPIS compilation failed:\n{details}")

    def _compiler_flags_for_build_dir(self, compiler_flags: list[str]):
        """Keep relative compiler paths relative to the campaign directory."""
        path_options = {"-I", "-L", "-include", "-iquote", "-isystem", "--sysroot"}
        joined_options = ("-I", "-L")
        normalized = []
        index = 0
        while index < len(compiler_flags):
            flag = compiler_flags[index]
            if flag in path_options and index + 1 < len(compiler_flags):
                normalized.extend([
                    flag,
                    self._absolute_flag_path(compiler_flags[index + 1]),
                ])
                index += 2
                continue
            if flag.startswith("--sysroot="):
                path = flag.split("=", 1)[1]
                normalized.append(f"--sysroot={self._absolute_flag_path(path)}")
                index += 1
                continue
            joined = next(
                (
                    option
                    for option in joined_options
                    if flag.startswith(option) and flag != option
                ),
                None,
            )
            if joined is not None:
                normalized.append(
                    f"{joined}{self._absolute_flag_path(flag[len(joined):])}"
                )
            else:
                normalized.append(flag)
            index += 1
        return normalized

    def _absolute_flag_path(self, path: str | Path):
        flag_path = Path(path)
        return str(flag_path if flag_path.is_absolute() else flag_path.resolve())

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
    campaign: Campaign
    log: list[str]

    def __init__(
        self,
        campaign: Campaign,
        environment: Environment,
        log_path: str | Path = "CampaignRun.log",
        debugger: str | None = None,
    ):
        self.campaign = campaign
        self.environment = environment
        self.log_path = Path(log_path)
        self.debugger = debugger
        self.log = []
        self.results: list[dict] = []

    def run_campaign(self, args: list[str] | None = None):
        self.log = []
        self.results = []
        for run in self.campaign.runs:
            self.log.append(f"RUN NAME: {run.name}")
            entry_source = self._entry_source(run.programs)
            if entry_source is None:
                self.log.append("No C or C++ source file found.")
                self.log.append("")
                continue

            run_args = args if args is not None else self._args_from_values_file(entry_source)
            self.log.append(f"Sources: {', '.join(run.programs)}")
            self.log.append(
                "Compiler flags: "
                f"{shlex.join(run.compiler_flags) if run.compiler_flags else '(none)'}"
            )
            self.log.append(
                f"Execution repetitions: {run.execution_repetitions}"
            )
            self.log.append(f"Args: {shlex.join(run_args) if run_args else '(none)'}")

            oracle_runner = OracleRunner(self.environment, self.debugger)
            aspis_runner = AspisRunner(
                self.environment,
                data_protection=run.data_protection,
                control_flow_protection=run.control_flow_protection,
                debugger=self.debugger,
            )

            try:
                # Build both variants before executing either one. This keeps the
                # selected ASPIS protection modes tied to this campaign run.
                oracle_runner.compile(run.programs, run.compiler_flags)
                aspis_runner.compile(run.programs, run.compiler_flags)

                oracle_results = oracle_runner.execute(
                    run_args, run.vars, run.execution_repetitions
                )
                aspis_results = aspis_runner.execute(
                    run_args, run.vars, run.execution_repetitions
                )
                differences = self._measurement_differences(
                    oracle_results, aspis_results
                )
                self.results.append(
                    {
                        "name": run.name,
                        "oracle": oracle_results,
                        "aspis": aspis_results,
                        "difference": differences,
                    }
                )
                self.log.extend(
                    self._comparison_lines(oracle_results, aspis_results, run.vars)
                )
            except Exception as error:
                self.log.append(f"ERROR: {error}")
            finally:
                oracle_runner.cleanup()
                aspis_runner.cleanup()
            self.log.append("")

        self.log_path.write_text("\n".join(self.log), encoding="utf-8")
        return self.log

    def _entry_source(self, programs: list[str]):
        return next((program for program in programs if Path(program).suffix in {".c", ".cpp", ".rs"}), None)

    def _args_from_values_file(self, source_file: str | Path):
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
        lines.extend(self._measurement_lines(oracle_results, aspis_results))

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

    def _measurement_differences(self, oracle_results: dict, aspis_results: dict):
        differences = {}
        for field in ("execution_time_seconds", "binary_size_bytes"):
            oracle_value = oracle_results[field]
            aspis_value = aspis_results[field]
            delta = aspis_value - oracle_value
            differences[field] = {
                "absolute": delta,
                "percent": (delta / oracle_value * 100) if oracle_value else None,
            }
        return differences

    def _measurement_lines(self, oracle_results: dict, aspis_results: dict):
        differences = self._measurement_differences(oracle_results, aspis_results)
        lines = ["measurements:"]
        lines.append(
            "  execution_repetitions: "
            f"oracle={oracle_results.get('execution_repetitions', 1)}, "
            f"aspis={aspis_results.get('execution_repetitions', 1)}"
        )
        lines.append(
            "  execution_time_standard_error_seconds: "
            f"oracle={oracle_results.get('execution_time_standard_error_seconds', 0.0)}, "
            f"aspis={aspis_results.get('execution_time_standard_error_seconds', 0.0)}"
        )
        for field, unit in (
            ("execution_time_seconds", "s"),
            ("binary_size_bytes", "bytes"),
        ):
            oracle_value = oracle_results[field]
            aspis_value = aspis_results[field]
            difference = differences[field]
            percent = difference["percent"]
            percent_text = "n/a" if percent is None else f"{percent:+.2f}%"
            lines.append(
                f"  {field}: oracle={oracle_value} {unit}, "
                f"aspis={aspis_value} {unit}, "
                f"difference={difference['absolute']:+} {unit} ({percent_text})"
            )
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
