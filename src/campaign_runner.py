from abc import ABC, abstractmethod
import concurrent.futures
import math
from pathlib import Path
import os
import platform
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time

from src.config_parser import Campaign, ControlFlowMode, DataProtectionMode
from src.constants import DEFAULT_TIMEOUT_MS
from src.env import Environment
from src.file_io import atomic_write_text
from src.injector import BaseInjector
from src.models import TerminationEvent


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
        measurement_repetitions: int = 100,
        functions_to_check: list[str] | None = None,
        measure_overhead: bool = True,
    ):
        """Execute an already compiled binary and collect its observable state."""
        if self._build_dir is None or not Path(self.exe).is_file():
            raise RuntimeError("The program must be compiled before it can be executed.")
        if measure_overhead and (type(measurement_repetitions) is not int or measurement_repetitions < 1):
            raise ValueError("measurement_repetitions must be a positive integer when measure_overhead is true.")

        args = args or []
        execution_times = []
        run_result = None
        
        reps = measurement_repetitions if measure_overhead else 1
        
        for _ in range(reps):
            started_at = time.perf_counter()
            try:
                run_result = subprocess.run(
                    [self._exe_command(), *args],
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

        if measure_overhead:
            mean = statistics.fmean(execution_times)
            standard_deviation = (
                statistics.stdev(execution_times)
                if reps > 1
                else 0.0
            )
            standard_error = standard_deviation / math.sqrt(reps) if reps > 0 else 0.0
        else:
            mean = 0.0
            standard_deviation = 0.0
            standard_error = 0.0

        self.results = {
            "stdout": run_result.stdout.strip(),
            "stderr": run_result.stderr.strip(),
            "returncode": run_result.returncode,
            "execution_time_seconds": mean,
            "execution_time_samples_seconds": execution_times,
            "execution_time_stddev_seconds": standard_deviation,
            "execution_time_standard_error_seconds": standard_error,
            "measurement_repetitions": reps,
            "binary_size_bytes": self.size,
        }
        function_names = list(dict.fromkeys(functions_to_check or ["main"]))
        snapshots = {
            function_name: self._debug_snapshot(
                args, variables_to_check, function_name
            )
            for function_name in function_names
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
        target_function: str = "main",
    ):
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
        target_function: str,
        args: list[str],
        variables_to_check: list[str] | None = None,
    ):
        variable_names = self._debug_variable_names(variables_to_check or [])
        target_function = self._debug_function_name(target_function)
        debugger_name = Path(debugger).name.lower()
        if "gdb" in debugger_name:
            flavor = "gdb"
        elif "lldb" in debugger_name:
            flavor = "lldb"
        else:
            raise ValueError(f"Unsupported debugger: {debugger}")
        
        run_env = os.environ.copy()
        run_env["TARGET_SRC"] = str(self.src)
        run_env["TARGET_FUNC"] = target_function
        run_env["VARS_TO_CHECK"] = ",".join(variable_names)
        
        base_dir = Path(__file__).parent / "gdb_scripts"
        
        if flavor == "gdb":
            script = base_dir / "snapshot_gdb.py"
            command = [
                debugger, "--batch", "-q",
                "-x", str(script),
                "--args", self.exe, *args
            ]
        elif flavor == "lldb":
            script = base_dir / "snapshot_lldb.py"
            command = [
                debugger, "--batch", "--no-lldbinit",
                "-o", f"command script import {shlex.quote(str(script))}",
                "--", self.exe, *args,
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

    def _debug_function_name(self, function_name: str):
        if re.fullmatch(r"[A-Za-z_]\w*", function_name) is None:
            raise ValueError(f"Invalid debugger function name: {function_name}")
        return function_name

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
        
        # Automatically inject default fault handlers so the user doesn't have to write them
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

    def run_campaign(self, args: list[str] | None = None, progress=None):
        import json
        from datetime import datetime

        self.log = []
        self.results = []
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.summary_path = self.log_path.with_name(
            f"campaign_summary_{timestamp}.json"
        )
        def report(phase):
            if progress is not None:
                progress({"test": run.name, "index": run_index,
                          "total": len(self.campaign.runs), "phase": phase,
                          "sources": ", ".join(run.programs)})

        oracle_runners_cache = {}
        aspis_runners_cache = {}
        owned_runners = []
        try:
            for run_index, run in enumerate(self.campaign.runs, start=1):
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
                self.log.append(f"Sources: {', '.join(run.programs)}")
                self.log.append(
                    "Compiler flags: "
                    f"{shlex.join(run.compiler_flags) if run.compiler_flags else '(none)'}"
                )
                self.log.append(
                    f"Execution repetitions: {run.execution_repetitions}"
                )
                self.log.append(f"Per-process timeout: {run.timeout_ms} ms")
                self.log.append(f"Args: {shlex.join(run_args) if run_args else '(none)'}")

                try:
                    if all(Path(p).suffix.lower() == ".rs" for p in run.programs):
                        raise ValueError("Configuration: Rust symbol selection is supported, but this runner requires a C/C++ compilation toolchain.")
                    for target in run.functions:
                        if re.fullmatch(r"[A-Za-z_]\w*", target.name) is None:
                            raise ValueError(f"Invalid debugger function name: {target.name}")
                    vars_to_check = [
                        var.name for tf in run.functions for var in tf.variables
                    ]
                    functions_to_check = [tf.name for tf in run.functions]
                    measurement_repetitions = (
                        (run.measurement_repetitions if run.measurement_repetitions is not None
                         else run.execution_repetitions) if run.measure_overhead else 1
                    )

                    # Define caching keys
                    oracle_cache_key = (
                        tuple(run.programs),
                        tuple(run.compiler_flags),
                        tuple(run_args),
                        tuple(vars_to_check),
                        tuple(functions_to_check),
                        run.measure_overhead,
                        measurement_repetitions,
                        run.timeout_ms,
                    )
                    aspis_cache_key = oracle_cache_key + (
                        run.data_protection.name,
                        run.control_flow_protection.name,
                    )

                    if oracle_cache_key in oracle_runners_cache:
                        oracle_runner = oracle_runners_cache[oracle_cache_key]
                        oracle_results = oracle_runner.results
                        report("Oracle execution loaded from cache")
                    else:
                        oracle_runner = OracleRunner(
                            self.environment, self.debugger, timeout_ms=run.timeout_ms
                        )
                        owned_runners.append(oracle_runner)
                        report("Compiling Oracle")
                        oracle_runner.compile(run.programs, run.compiler_flags)
                        report(f"Oracle execution (measurements) and debugger snapshot")
                        oracle_results = oracle_runner.execute(
                            run_args,
                            vars_to_check,
                            measurement_repetitions,
                            functions_to_check,
                            measure_overhead=run.measure_overhead,
                        )
                        oracle_runners_cache[oracle_cache_key] = oracle_runner

                    if aspis_cache_key in aspis_runners_cache:
                        aspis_runner = aspis_runners_cache[aspis_cache_key]
                        aspis_results = aspis_runner.results
                        report("ASPIS execution loaded from cache")
                    else:
                        aspis_runner = AspisRunner(
                            self.environment,
                            data_protection=run.data_protection,
                            control_flow_protection=run.control_flow_protection,
                            debugger=self.debugger,
                            timeout_ms=run.timeout_ms,
                        )
                        owned_runners.append(aspis_runner)
                        report("Compiling ASPIS")
                        aspis_runner.compile(run.programs, run.compiler_flags)
                        report(f"ASPIS execution (measurements) and debugger snapshot")
                        aspis_results = aspis_runner.execute(
                            run_args,
                            vars_to_check,
                            measurement_repetitions,
                            functions_to_check,
                            measure_overhead=run.measure_overhead,
                        )
                        aspis_runners_cache[aspis_cache_key] = aspis_runner

                    differences = self._measurement_differences(
                        oracle_results, aspis_results
                    ) if run.measure_overhead else {}
                    injections = []
                    def _run_inj(inj):
                        start_count = len(injections)
                        failures = []
                        def record(result):
                            if result.event == TerminationEvent.INTERNAL_ERROR:
                                detail = (result.state.error or result.state.stderr
                                          or result.state.debugger_output[-500:])
                                raise RuntimeError(f"Debugger failed during injection: {detail}")
                            injections.append((result, target_function))
                        def injection_progress(label):
                            report(f"{label}: {target_function.name} · "
                                   f"{len(injections) - start_count}/{run.execution_repetitions} injections "
                                   f"· {len(failures)} failed attempts")
                        injection_progress("Profiling / fault injection")
                        with concurrent.futures.ThreadPoolExecutor() as executor:
                            futures = [
                                executor.submit(inj.run, run_args)
                                for _ in range(run.execution_repetitions)
                            ]
                            for future in concurrent.futures.as_completed(futures):
                                try:
                                    result = future.result()
                                    record(result)
                                    self.log.append(f"Injection result: {result}")
                                except Exception as e:
                                    failures.append(e)
                                injection_progress("Fault injection")

                        # Debugger startup/profiling can fail transiently under a
                        # highly concurrent campaign. Replace failed attempts so
                        # every result always contains the configured sample size.
                        retry_budget = run.execution_repetitions
                        while (
                            len(injections) - start_count < run.execution_repetitions
                            and retry_budget > 0
                        ):
                            retry_budget -= 1
                            injection_progress("Retrying injection")
                            try:
                                result = inj.run(run_args)
                                record(result)
                                self.log.append(f"Injection retry result: {result}")
                            except Exception as error:
                                failures.append(error)
                            injection_progress("Fault injection")
                        completed = len(injections) - start_count
                        if completed != run.execution_repetitions:
                            details = str(failures[-1]) if failures else "unknown error"
                            raise RuntimeError(
                                "Injection campaign incomplete: "
                                f"expected {run.execution_repetitions}, got {completed}. "
                                f"Last error: {details}"
                            )
                        if failures:
                            self.log.append(
                                f"Recovered {len(failures)} transient injection error(s)."
                            )

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
                            _run_inj(injector)

                    report("Classifying results")
                    # Classify injection results
                    injection_summary = {
                        "total": len(injections),
                        "masked": 0,
                        "sdc": 0,
                        "latent": 0,
                        "timeout": 0,
                        "crash": 0,
                        "aspis_data": 0,
                        "aspis_cf": 0,
                        "sdc_states": [],
                        "latent_states": []
                    }
                
                    for inj_result, injected_function in injections:
                        oracle_vars = oracle_results.get("snapshots", {}).get(
                            injected_function.name, oracle_results
                        ).get("variables", {})
                        event = inj_result.event
                        if event == TerminationEvent.TIMEOUT:
                            injection_summary["timeout"] += 1
                        elif event == TerminationEvent.CRASHED:
                            injection_summary["crash"] += 1
                        elif event == TerminationEvent.ASPIS_DATA:
                            injection_summary["aspis_data"] += 1
                        elif event == TerminationEvent.ASPIS_CF:
                            injection_summary["aspis_cf"] += 1
                        elif event == TerminationEvent.RETURNED:
                            # Check if state matches oracle
                            state = inj_result.state
                            output_differs = False
                            state_differs = False
                        
                            if state.stdout != oracle_results.get("stdout", ""):
                                output_differs = True
                            elif state.stderr != oracle_results.get("stderr", ""):
                                output_differs = True
                            
                            for variable in injected_function.variables:
                                var_name = variable.name
                                if state.variables.get(var_name) != oracle_vars.get(var_name):
                                    state_differs = True
                                    break
                                
                            if output_differs:
                                injection_summary["sdc"] += 1
                                # Save state as dict
                                from dataclasses import asdict
                                injection_summary["sdc_states"].append({**asdict(state), "function": injected_function.name})
                            elif state_differs:
                                injection_summary["latent"] += 1
                                from dataclasses import asdict
                                injection_summary["latent_states"].append({**asdict(state), "function": injected_function.name})
                            else:
                                injection_summary["masked"] += 1

                    self.results.append(
                        {
                            "name": run.name,
                            "timeout_ms": run.timeout_ms,
                            "oracle": oracle_results,
                            "aspis": aspis_results,
                            "difference": differences,
                            "injections": injection_summary,
                        }
                    )

                    report("Saving analysis")
                    # Save results to JSON
                    atomic_write_text(self.summary_path, json.dumps(self.results, indent=2))

                    self.log.extend(
                        self._comparison_lines(oracle_results, aspis_results, vars_to_check, run.measure_overhead)
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

    def _comparison_lines(self, oracle_results: dict, aspis_results: dict, vars_to_check: list[str], measure_overhead: bool = True):
        lines = ["Comparison:"]
        lines.extend(self._field_comparison("returncode", oracle_results, aspis_results))
        lines.extend(self._field_comparison("stdout", oracle_results, aspis_results))
        lines.extend(self._field_comparison("stderr", oracle_results, aspis_results))
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
            "  measurement_repetitions: "
            f"oracle={oracle_results.get('measurement_repetitions', 1)}, "
            f"aspis={aspis_results.get('measurement_repetitions', 1)}"
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
