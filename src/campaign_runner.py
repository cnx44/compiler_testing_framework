from abc import ABC, abstractmethod
import concurrent.futures
from dataclasses import asdict
from datetime import datetime
import json
import math
from pathlib import Path
import os
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time

from src import debug_output
from src.config_parser import Campaign, ControlFlowMode, DataProtectionMode
from src.constants import DEFAULT_TIMEOUT_MS
from src.env import Environment
from src.file_io import atomic_write_text
from src.injector import BaseInjector
from src.models import TerminationEvent

IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
GDB_SCRIPTS_DIR = Path(__file__).parent / "gdb_scripts"

ASPIS_DATA_FLAGS = {
    DataProtectionMode.EDDI: ["--eddi"],
    DataProtectionMode.SEDDI: ["--seddi"],
    DataProtectionMode.FDSC: ["--fdsc"],
    DataProtectionMode.NONE: ["--no-dup"],
}
ASPIS_CONTROL_FLAGS = {
    ControlFlowMode.CFCSS: ["--cfcss"],
    ControlFlowMode.RASM: ["--rasm"],
    ControlFlowMode.INTER_RASM: ["--inter-rasm"],
    ControlFlowMode.NONE: ["--no-cfc"],
}
# Compiler options whose next argument is a path.
PATH_OPTIONS = {"-I", "-L", "-include", "-iquote", "-isystem", "--sysroot"}


class ProgramRunner(ABC):
    """Build, execute, and inspect a program with a configured toolchain."""

    def __init__(
        self,
        environment: Environment,
        debugger: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        self.environment = environment
        self.debugger = debugger
        self.timeout_ms = timeout_ms
        self.src = ""
        self.exe = ""
        self.results: dict = {}
        self.size = 0
        self.var_lengths: dict[str, str] = {}
        self._build_dir: tempfile.TemporaryDirectory[str] | None = None

    def compile(
        self,
        programs: list[str],
        compiler_flags: list[str] | None = None,
    ):
        """Compile ``programs`` and keep the resulting binary available."""
        sources = self._source_files(programs)
        self.src = str(sources[0])
        self.cleanup()
        self._build_dir = tempfile.TemporaryDirectory(prefix="program-run-")
        self.exe = str(Path(self._build_dir.name) / "program")

        try:
            self._compile(sources, compiler_flags or [])
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
            self.cleanup()
            raise

    def execute(
        self,
        args: list[str] | None = None,
        variables_to_check: list[str] | None = None,
        measurement_repetitions: int = 100,
        functions_to_check: list[str] | None = None,
        measure_overhead: bool = True,
        var_lengths: dict[str, str] | None = None,
    ):
        """Execute an already compiled binary and collect its observable state."""
        if self._build_dir is None or not Path(self.exe).is_file():
            raise RuntimeError("The program must be compiled before it can be executed.")
        if measure_overhead and (type(measurement_repetitions) is not int or measurement_repetitions < 1):
            raise ValueError("measurement_repetitions must be a positive integer when measure_overhead is true.")

        self.var_lengths = var_lengths or {}
        args = args or []
        repetitions = measurement_repetitions if measure_overhead else 1

        execution_times = []
        for _ in range(repetitions):
            started_at = time.perf_counter()
            try:
                run_result = subprocess.run(
                    [self.exe, *args],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_ms / 1000.0,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    f"Program execution timed out after {self.timeout_ms} ms."
                ) from error
            execution_times.append(time.perf_counter() - started_at)

        mean = standard_deviation = standard_error = 0.0
        if measure_overhead:
            mean = statistics.fmean(execution_times)
            if repetitions > 1:
                standard_deviation = statistics.stdev(execution_times)
            standard_error = standard_deviation / math.sqrt(repetitions)

        self.results = {
            "stdout": run_result.stdout.strip(),
            "stderr": run_result.stderr.strip(),
            "returncode": run_result.returncode,
            "execution_time_seconds": mean,
            "execution_time_samples_seconds": execution_times,
            "execution_time_stddev_seconds": standard_deviation,
            "execution_time_standard_error_seconds": standard_error,
            "measurement_repetitions": repetitions,
            "binary_size_bytes": self.size,
        }

        snapshots = {
            function_name: self._debug_snapshot(args, variables_to_check, function_name)
            for function_name in dict.fromkeys(functions_to_check or ["main"])
        }
        variables = {}
        registers = {}
        for snapshot in snapshots.values():
            variables.update(snapshot["variables"])
            registers.update(snapshot["registers"])
        self.results.update(
            {
                "variables": variables,
                "registers": registers,
                "snapshots": snapshots,
            }
        )
        return self.results

    def run(
        self,
        programs: list[str],
        args: list[str] | None = None,
        compiler_flags: list[str] | None = None,
        variables_to_check: list[str] | None = None,
        measurement_repetitions: int = 1,
        functions_to_check: list[str] | None = None,
        measure_overhead: bool = True,
        var_lengths: dict[str, str] | None = None,
    ):
        """Compile and execute a program, cleaning temporary artifacts afterwards."""
        try:
            self.compile(programs, compiler_flags)
            return self.execute(
                args,
                variables_to_check,
                measurement_repetitions,
                functions_to_check,
                measure_overhead=measure_overhead,
                var_lengths=var_lengths,
            )
        finally:
            self.cleanup()

    def cleanup(self):
        """Release the temporary build directory, if present."""
        if self._build_dir is not None:
            self._build_dir.cleanup()
            self._build_dir = None

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
        target_function: str = "main",
    ):
        """Stop at the end of ``target_function`` and read its variables and registers."""
        target_function = self._debug_function_name(target_function)
        errors = []
        for debugger in self._debuggers():
            try:
                debug_result = self._run_debugger(
                    debugger, target_function, args, variables_to_check
                )
            except subprocess.TimeoutExpired:
                errors.append(
                    f"{debugger}: snapshot timed out after {self.timeout_ms} ms"
                )
                continue
            if debug_result.returncode != 0:
                errors.append(f"{debugger}: {debug_result.stderr.strip()}")
                continue

            output = debug_result.stdout
            locals_end = "@@GLOBALS@@" if "@@GLOBALS@@" in output else "@@REGISTERS@@"
            variables = {
                **self._parse_debug_variables(output, "@@ARGS@@", "@@LOCALS@@"),
                **self._parse_debug_variables(output, "@@LOCALS@@", locals_end),
                **self._parse_debug_variables(output, "@@GLOBALS@@", "@@REGISTERS@@"),
            }
            registers = self._parse_debug_registers(output)
            if registers:
                return {"variables": variables, "registers": registers}
            errors.append(f"{debugger}: no registers were found")

        details = "\n".join(errors) or "gdb is not installed."
        raise RuntimeError(f"Debug snapshot failed:\n{details}")

    def _debuggers(self):
        debugger = self.debugger if self.debugger is not None else self.environment.gdb_executable
        return [debugger] if shutil.which(debugger) else []

    def _run_debugger(
        self,
        debugger: str,
        target_function: str,
        args: list[str],
        variables_to_check: list[str] | None = None,
    ):
        variable_names = self._debug_variable_names(variables_to_check or [])
        target_function = self._debug_function_name(target_function)
        if "gdb" not in Path(debugger).name.lower():
            raise ValueError(f"Unsupported debugger: {debugger}")

        run_env = os.environ.copy()
        run_env["TARGET_SRC"] = str(self.src)
        run_env["TARGET_FUNC"] = target_function
        run_env["VARS_TO_CHECK"] = ",".join(variable_names)
        if self.var_lengths:
            run_env["VAR_LENGTHS"] = ",".join(f"{k}:{v}" for k, v in self.var_lengths.items())

        command = [
            debugger, "--batch", "-q",
            "-x", str(GDB_SCRIPTS_DIR / "snapshot_gdb.py"),
            "--args", self.exe, *args,
        ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=run_env,
            timeout=self.timeout_ms / 1000.0,
            stdin=subprocess.DEVNULL,
        )

    def _debug_variable_names(self, variable_names: list[str]):
        invalid = [name for name in variable_names if IDENTIFIER.fullmatch(name) is None]
        if invalid:
            raise ValueError(
                f"Invalid debugger variable name(s): {', '.join(invalid)}"
            )
        return variable_names

    def _debug_function_name(self, function_name: str):
        if IDENTIFIER.fullmatch(function_name) is None:
            raise ValueError(f"Invalid debugger function name: {function_name}")
        return function_name

    def _parse_debug_variables(self, output: str, start: str, end: str):
        return debug_output.parse_variables(
            self._section(output, start, end), keep_empty=False
        )

    def _parse_debug_registers(self, output: str):
        return debug_output.parse_registers(self._section(output, "@@REGISTERS@@", None))

    def _section(self, output: str, start: str, end: str | None):
        return debug_output.section(output, start, end) or ""


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
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        super().__init__(environment, debugger, timeout_ms)
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
        command.extend(self.environment.aspis_flags)
        command.extend(str(source.resolve()) for source in sources)

        # Link the default fault handlers so the user doesn't have to write them.
        handlers_path = Path(__file__).parent / "aspis_handlers.c"
        if handlers_path.is_file():
            command.append(str(handlers_path.resolve()))

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
        """Keep relative compiler paths relative to the campaign directory.

        ASPIS runs inside its temporary build directory, so path arguments
        (``-I dir``, ``-Idir``, ``--sysroot=dir``, ...) are made absolute.
        """
        normalized = []
        remaining = iter(compiler_flags)
        for flag in remaining:
            if flag in PATH_OPTIONS:
                path = next(remaining, None)
                normalized.append(flag)
                if path is not None:
                    normalized.append(self._absolute_flag_path(path))
            elif flag.startswith("--sysroot="):
                path = flag.split("=", 1)[1]
                normalized.append(f"--sysroot={self._absolute_flag_path(path)}")
            elif flag[:2] in ("-I", "-L") and len(flag) > 2:
                normalized.append(flag[:2] + self._absolute_flag_path(flag[2:]))
            else:
                normalized.append(flag)
        return normalized

    def _absolute_flag_path(self, path: str | Path):
        flag_path = Path(path)
        return str(flag_path if flag_path.is_absolute() else flag_path.resolve())

    def _aspis_mode_args(self):
        return ASPIS_DATA_FLAGS[self.data_protection] + ASPIS_CONTROL_FLAGS[self.control_flow_protection]


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

    def run_campaign(self, args: list[str] | None = None, progress=None):
        """Run every test of the campaign, writing the log and a JSON summary.

        ``progress``, when given, is called with a dict describing each phase.
        """
        self.log = []
        self.results = []
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.summary_path = self.log_path.with_name(
            f"{self.log_path.stem}_summary_{timestamp}.json"
        )

        # Identical Oracle/ASPIS builds are shared between tests of the campaign.
        oracle_cache = {}
        aspis_cache = {}
        owned_runners = []
        try:
            for run_index, run in enumerate(self.campaign.runs, start=1):
                def report(phase):
                    if progress is not None:
                        progress({"test": run.name, "index": run_index,
                                  "total": len(self.campaign.runs), "phase": phase,
                                  "sources": ", ".join(run.programs)})

                report("Preparing test")
                self.log.append(f"RUN NAME: {run.name}")
                entry_source = self._entry_source(run.programs)
                if entry_source is None:
                    self.log.append(f"ERROR: CONFIGURATION [{run.name}]: No C or C++ source file found.")
                    self.log.append("")
                    continue

                try:
                    run_args = args if args is not None else self._args_from_values_file(entry_source)
                except (OSError, UnicodeError) as error:
                    self.log.append(f"ERROR: CONFIGURATION [{run.name}]: Unable to read arguments: {error}")
                    continue
                compiler_flags = shlex.join(run.compiler_flags) if run.compiler_flags else "(none)"
                self.log.append(f"Sources: {', '.join(run.programs)}")
                self.log.append(f"Compiler flags: {compiler_flags}")
                self.log.append(f"Execution repetitions: {run.execution_repetitions}")
                self.log.append(f"Per-process timeout: {run.timeout_ms} ms")
                self.log.append(f"Args: {shlex.join(run_args) if run_args else '(none)'}")

                try:
                    self._run_test(
                        run, run_args, entry_source, report,
                        oracle_cache, aspis_cache, owned_runners,
                    )
                except (ValueError, FileNotFoundError) as error:
                    self.log.append(f"ERROR: CONFIGURATION [{run.name}]: {error}")
                except Exception as error:
                    self.log.append(f"ERROR: {error}")
                self.log.append("")
        finally:
            for runner in owned_runners:
                runner.cleanup()
            self.log_path.write_text("\n".join(self.log), encoding="utf-8")
        return self.log

    def _run_test(
        self, run, run_args, entry_source, report,
        oracle_cache, aspis_cache, owned_runners,
    ):
        """Build and run Oracle and ASPIS, inject faults, and record the results."""
        if all(Path(p).suffix.lower() == ".rs" for p in run.programs):
            raise ValueError("Configuration: Rust symbol selection is supported, but this runner requires a C/C++ compilation toolchain.")
        for target in run.functions:
            if IDENTIFIER.fullmatch(target.name) is None:
                raise ValueError(f"Invalid debugger function name: {target.name}")

        vars_to_check = [var.name for tf in run.functions for var in tf.variables]
        var_lengths = {
            var.name: str(var.length)
            for tf in run.functions for var in tf.variables if var.length
        }
        functions_to_check = [tf.name for tf in run.functions]
        if not run.measure_overhead:
            measurement_repetitions = 1
        elif run.measurement_repetitions is not None:
            measurement_repetitions = run.measurement_repetitions
        else:
            measurement_repetitions = run.execution_repetitions

        execute_args = (run_args, vars_to_check, measurement_repetitions, functions_to_check)
        execute_kwargs = {"measure_overhead": run.measure_overhead}
        if var_lengths:
            execute_kwargs["var_lengths"] = var_lengths

        oracle_key = (
            tuple(run.programs),
            tuple(run.compiler_flags),
            tuple(run_args),
            tuple(vars_to_check),
            tuple(functions_to_check),
            run.measure_overhead,
            measurement_repetitions,
            run.timeout_ms,
        )
        aspis_key = oracle_key + (
            run.data_protection.name,
            run.control_flow_protection.name,
        )

        _, oracle_results = self._build_and_execute(
            "Oracle", oracle_cache, oracle_key, owned_runners, run, report,
            lambda: OracleRunner(self.environment, self.debugger, timeout_ms=run.timeout_ms),
            execute_args, execute_kwargs,
        )
        aspis_runner, aspis_results = self._build_and_execute(
            "ASPIS", aspis_cache, aspis_key, owned_runners, run, report,
            lambda: AspisRunner(
                self.environment,
                data_protection=run.data_protection,
                control_flow_protection=run.control_flow_protection,
                debugger=self.debugger,
                timeout_ms=run.timeout_ms,
            ),
            execute_args, execute_kwargs,
        )

        differences = (
            self._measurement_differences(oracle_results, aspis_results)
            if run.measure_overhead else {}
        )

        injections = []
        for target_function in run.functions:
            injectors = BaseInjector.create_for_target(
                run.injection,
                self.environment,
                aspis_runner.exe,
                entry_source,
                target_function,
                debugger=self.debugger,
                timeout_ms=run.timeout_ms,
            )
            for injector in injectors:
                results = self._run_injector(injector, run, run_args, target_function, report)
                injections.extend((result, target_function) for result in results)

        report("Classifying results")
        self.results.append(
            {
                "name": run.name,
                "timeout_ms": run.timeout_ms,
                "oracle": oracle_results,
                "aspis": aspis_results,
                "difference": differences,
                "injections": self._classify_injections(injections, oracle_results),
            }
        )

        report("Saving analysis")
        atomic_write_text(self.summary_path, json.dumps(self.results, indent=2))

        self.log.extend(
            self._comparison_lines(oracle_results, aspis_results, vars_to_check, run.measure_overhead)
        )

    def _build_and_execute(
        self, label, cache, key, owned_runners, run, report,
        create_runner, execute_args, execute_kwargs,
    ):
        """Return ``(runner, results)``, reusing a cached runner when possible."""
        if key in cache:
            runner = cache[key]
            report(f"{label} execution loaded from cache")
            return runner, runner.results

        runner = create_runner()
        owned_runners.append(runner)
        report(f"Compiling {label}")
        runner.compile(run.programs, run.compiler_flags)
        report(f"{label} execution (measurements) and debugger snapshot")
        results = runner.execute(*execute_args, **execute_kwargs)
        cache[key] = runner
        return runner, results

    def _run_injector(self, injector, run, run_args, target_function, report):
        """Run ``run.execution_repetitions`` injections concurrently.

        Debugger startup/profiling can fail transiently under a highly
        concurrent campaign, so failed attempts are retried (up to as many
        extra attempts as repetitions) to always reach the configured sample
        size. A debugger that fails *during* an injection is not retried.
        """
        wanted = run.execution_repetitions
        results = []
        failures = []

        def record(result):
            if result.event == TerminationEvent.INTERNAL_ERROR:
                detail = (result.state.error or result.state.stderr
                          or result.state.debugger_output[-500:])
                raise RuntimeError(f"Debugger failed during injection: {detail}")
            results.append(result)

        def show_progress(label):
            report(f"{label}: {target_function.name} · "
                   f"{len(results)}/{wanted} injections "
                   f"· {len(failures)} failed attempts")

        show_progress("Profiling / fault injection")
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(injector.run, run_args) for _ in range(wanted)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    record(result)
                    self.log.append(f"Injection result: {result}")
                except Exception as error:
                    failures.append(error)
                show_progress("Fault injection")

        retry_budget = wanted
        while len(results) < wanted and retry_budget > 0:
            retry_budget -= 1
            show_progress("Retrying injection")
            try:
                result = injector.run(run_args)
                record(result)
                self.log.append(f"Injection retry result: {result}")
            except Exception as error:
                failures.append(error)
            show_progress("Fault injection")

        if len(results) != wanted:
            details = str(failures[-1]) if failures else "unknown error"
            raise RuntimeError(
                "Injection campaign incomplete: "
                f"expected {wanted}, got {len(results)}. "
                f"Last error: {details}"
            )
        if failures:
            self.log.append(f"Recovered {len(failures)} transient injection error(s).")
        return results

    def _classify_injections(self, injections, oracle_results: dict):
        """Count the outcome of each ``(result, target_function)`` injection.

        A run that returns normally is an SDC when its output differs from the
        Oracle, latent when only a selected variable differs, masked otherwise.
        """
        summary = {
            "total": len(injections),
            "masked": 0,
            "sdc": 0,
            "latent": 0,
            "timeout": 0,
            "crash": 0,
            "aspis_data": 0,
            "aspis_cf": 0,
            "sdc_states": [],
            "latent_states": [],
        }
        counters = {
            TerminationEvent.TIMEOUT: "timeout",
            TerminationEvent.CRASHED: "crash",
            TerminationEvent.ASPIS_DATA: "aspis_data",
            TerminationEvent.ASPIS_CF: "aspis_cf",
        }
        for result, function in injections:
            if result.event in counters:
                summary[counters[result.event]] += 1
                continue
            if result.event != TerminationEvent.RETURNED:
                continue

            state = result.state
            oracle_vars = oracle_results.get("snapshots", {}).get(
                function.name, oracle_results
            ).get("variables", {})
            output_differs = (
                state.stdout != oracle_results.get("stdout", "")
                or state.stderr != oracle_results.get("stderr", "")
            )
            state_differs = any(
                self._values_differ(
                    str(state.variables.get(variable.name, "")),
                    str(oracle_vars.get(variable.name, "")),
                )
                for variable in function.variables
            )
            if output_differs:
                summary["sdc"] += 1
                summary["sdc_states"].append({**asdict(state), "function": function.name})
            elif state_differs:
                summary["latent"] += 1
                summary["latent_states"].append({**asdict(state), "function": function.name})
            else:
                summary["masked"] += 1
        return summary

    @staticmethod
    def _values_differ(injected: str, expected: str) -> bool:
        # Two different addresses are not a corruption: ASPIS changes the layout.
        if injected == expected:
            return False
        return not (HEX_ADDRESS.fullmatch(injected) and HEX_ADDRESS.fullmatch(expected))

    def _entry_source(self, programs: list[str]):
        return next((program for program in programs if Path(program).suffix in {".c", ".cpp", ".rs"}), None)

    def _args_from_values_file(self, source_file: str | Path):
        """Read ``args[N] = value`` lines from the ``.values`` file next to the source."""
        values_path = Path(source_file).with_suffix(".values")
        if not values_path.is_file():
            return []

        args = {}
        for line in values_path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*args\[(\d+)\]\s*=\s*(.+?)\s*$", line)
            if match:
                args[int(match.group(1))] = match.group(2)
        return [args[index] for index in sorted(args)]

    def _comparison_lines(self, oracle_results: dict, aspis_results: dict, vars_to_check: list[str], measure_overhead: bool = True):
        lines = ["Comparison:"]
        for field in ("returncode", "stdout", "stderr"):
            lines.extend(self._field_comparison(field, oracle_results, aspis_results))
        if measure_overhead:
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
            delta = aspis_results[field] - oracle_value
            differences[field] = {
                "absolute": delta,
                "percent": (delta / oracle_value * 100) if oracle_value else None,
            }
        return differences

    def _measurement_lines(self, oracle_results: dict, aspis_results: dict):
        differences = self._measurement_differences(oracle_results, aspis_results)
        lines = [
            "measurements:",
            "  measurement_repetitions: "
            f"oracle={oracle_results.get('measurement_repetitions', 1)}, "
            f"aspis={aspis_results.get('measurement_repetitions', 1)}",
            "  execution_time_standard_error_seconds: "
            f"oracle={oracle_results.get('execution_time_standard_error_seconds', 0.0)}, "
            f"aspis={aspis_results.get('execution_time_standard_error_seconds', 0.0)}",
        ]
        for field, unit in (
            ("execution_time_seconds", "s"),
            ("binary_size_bytes", "bytes"),
        ):
            difference = differences[field]
            percent = difference["percent"]
            percent_text = "n/a" if percent is None else f"{percent:+.2f}%"
            lines.append(
                f"  {field}: oracle={oracle_results[field]} {unit}, "
                f"aspis={aspis_results[field]} {unit}, "
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
