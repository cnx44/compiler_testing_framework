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
from src.config_parser import Campaign, ControlFlowMode, DataProtectionMode, RunnerTest, TargetFunction
from src.config_parser import DEFAULT_TIMEOUT_MS
from src.env import Environment
from src.file_io import atomic_write_text
from src.injector import GDB_SCRIPTS_DIR, create_injectors, dump_env, gdb_command
from src.models import TerminationEvent

IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
HANDLERS_SOURCE = Path(__file__).parent / "aspis_handlers.c"

ASPIS_DATA_FLAGS = {
    DataProtectionMode.EDDI: "--eddi",
    DataProtectionMode.SEDDI: "--seddi",
    DataProtectionMode.FDSC: "--fdsc",
    DataProtectionMode.NONE: "--no-dup",
}
ASPIS_CONTROL_FLAGS = {
    ControlFlowMode.CFCSS: "--cfcss",
    ControlFlowMode.RASM: "--rasm",
    ControlFlowMode.INTER_RASM: "--inter-rasm",
    ControlFlowMode.NONE: "--no-cfc",
}
# Compiler options whose next argument is a path.
PATH_OPTIONS = {"-I", "-L", "-include", "-iquote", "-isystem", "--sysroot"}


def source_files(programs: list[str]) -> list[Path]:
    """Return the C/C++ sources of a run, checking that they can be compiled."""
    sources = [Path(program) for program in programs]
    unsupported = [str(s) for s in sources if s.suffix not in {".c", ".cpp"}]
    if unsupported:
        raise ValueError(f"Only C and C++ sources can be compiled: {', '.join(unsupported)}")
    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        raise FileNotFoundError(f"Source file(s) not found: {', '.join(missing)}")
    return sources


def args_from_values_file(source: Path) -> list[str]:
    """Read ``args[N] = value`` lines from the ``.values`` file next to the source."""
    values_path = source.with_suffix(".values")
    if not values_path.is_file():
        return []
    args = {}
    for line in values_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*args\[(\d+)\]\s*=\s*(.+?)\s*$", line)
        if match:
            args[int(match.group(1))] = match.group(2)
    return [args[index] for index in sorted(args)]


class ProgramRunner(ABC):
    """Build, execute, and inspect a program with a configured toolchain."""

    def __init__(self, environment: Environment, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.environment = environment
        self.timeout_ms = timeout_ms
        self.exe = ""
        self.size = 0
        self._build_dir: tempfile.TemporaryDirectory[str] | None = None

    def compile(self, sources: list[Path], compiler_flags: list[str] = ()):
        """Compile ``sources`` into a binary kept until ``cleanup()``."""
        self.cleanup()
        self._build_dir = tempfile.TemporaryDirectory(prefix="program-run-")
        self.exe = str(Path(self._build_dir.name) / "program")
        try:
            self._compile(sources, list(compiler_flags))
            if not Path(self.exe).is_file():
                raise RuntimeError(f"Compilation did not emit the expected binary: {self.exe}")
            self.size = Path(self.exe).stat().st_size
        except Exception:
            self.cleanup()
            raise

    def cleanup(self):
        if self._build_dir is not None:
            self._build_dir.cleanup()
            self._build_dir = None

    @abstractmethod
    def _compile(self, sources: list[Path], compiler_flags: list[str]):
        """Compile ``sources`` and emit the executable at ``self.exe``."""

    def execute(self, args: list[str], functions: list[TargetFunction], repetitions: int = 1) -> dict:
        """Run the binary ``repetitions`` times, then snapshot every function's return."""
        if self._build_dir is None:
            raise RuntimeError("The program must be compiled before it can be executed.")
        times = []
        for _ in range(repetitions):
            started_at = time.perf_counter()
            try:
                process = subprocess.run(
                    [self.exe, *args],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_ms / 1000,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(f"Program execution timed out after {self.timeout_ms} ms.") from error
            times.append(time.perf_counter() - started_at)

        stddev = statistics.stdev(times) if repetitions > 1 else 0.0
        snapshots = {function.name: self.snapshot(args, function) for function in functions}
        return {
            "stdout": process.stdout.strip(),
            "stderr": process.stderr.strip(),
            "returncode": process.returncode,
            "execution_time_seconds": statistics.fmean(times),
            "execution_time_samples_seconds": times,
            "execution_time_stddev_seconds": stddev,
            "execution_time_standard_error_seconds": stddev / math.sqrt(repetitions),
            "measurement_repetitions": repetitions,
            "binary_size_bytes": self.size,
            # Variables of all snapshots, for the textual comparison.
            "variables": {
                name: value
                for snapshot in snapshots.values()
                for name, value in snapshot["variables"].items()
            },
            "snapshots": snapshots,
        }

    def snapshot(self, args: list[str], function: TargetFunction) -> dict:
        """Stop at the end of ``function`` and read its variables and registers."""
        gdb = self.environment.gdb_executable
        if shutil.which(gdb) is None:
            raise RuntimeError(f"Debug snapshot failed: {gdb} is not installed.")
        try:
            process = subprocess.run(
                gdb_command(gdb, GDB_SCRIPTS_DIR / "snapshot_gdb.py", self.exe, args),
                capture_output=True,
                text=True,
                errors="replace",
                env={**os.environ, **dump_env(function)},
                timeout=self.timeout_ms / 1000,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Debug snapshot of {function.name} timed out after {self.timeout_ms} ms.")
        dump = debug_output.parse_dump(process.stdout)
        if process.returncode != 0 or dump is None:
            details = process.stderr.strip() or process.stdout.strip()[-1000:]
            raise RuntimeError(f"Debug snapshot of {function.name} failed:\n{details}")
        variables, registers = dump
        return {"variables": variables, "registers": registers}


class OracleRunner(ProgramRunner):
    """Runs the unprotected reference program with the vanilla compiler."""

    def _compile(self, sources: list[Path], compiler_flags: list[str]):
        compiler = self.environment.vanilla_compiler
        if not compiler:
            raise RuntimeError("Environment.vanilla_compiler is not configured.")
        result = subprocess.run(
            [compiler, "-g", "-O0", *map(str, sources), *compiler_flags, "-o", self.exe],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Vanilla compilation failed:\n{result.stderr}")


class AspisRunner(ProgramRunner):
    """Runs the protected program built through the ASPIS toolchain."""

    def __init__(
        self,
        environment: Environment,
        data_protection: DataProtectionMode = DataProtectionMode.EDDI,
        control_flow_protection: ControlFlowMode = ControlFlowMode.CFCSS,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        super().__init__(environment, timeout_ms)
        self.data_protection = data_protection
        self.control_flow_protection = control_flow_protection

    def _compile(self, sources: list[Path], compiler_flags: list[str]):
        aspis_path = self.environment.aspis_path
        if not aspis_path or not Path(aspis_path).is_file():
            raise RuntimeError("Environment.aspis_path must point to an ASPIS script.")

        build_dir = Path(self.exe).parent
        command = [str(Path(aspis_path).resolve()), "-g", "-o", "program", "--build-dir", str(build_dir)]
        if self.environment.llvm_path:
            command += ["--llvm-bin", str(self.environment.llvm_path)]
        command += [
            ASPIS_DATA_FLAGS[self.data_protection],
            ASPIS_CONTROL_FLAGS[self.control_flow_protection],
            *self.environment.aspis_flags,
            *(str(source.resolve()) for source in sources),
            # Default fault handlers, so that programs don't have to define them.
            str(HANDLERS_SOURCE),
            *absolute_flag_paths(compiler_flags),
        ]
        result = subprocess.run(command, capture_output=True, text=True, cwd=build_dir)
        if result.returncode != 0:
            details = "\n".join(part for part in (result.stdout, result.stderr) if part)
            raise RuntimeError(f"ASPIS compilation failed:\n{details}")


def absolute_flag_paths(compiler_flags: list[str]) -> list[str]:
    """Make the paths in ``-I dir``, ``-Idir``, ``--sysroot=dir``, ... absolute.

    ASPIS runs inside its temporary build directory, while relative paths are
    meant relative to the campaign directory (the current one).
    """
    def absolute(path):
        return str(Path(path).resolve())

    normalized = []
    flags = iter(compiler_flags)
    for flag in flags:
        if flag in PATH_OPTIONS:
            path = next(flags, None)
            normalized += [flag] if path is None else [flag, absolute(path)]
        elif flag.startswith("--sysroot="):
            normalized.append("--sysroot=" + absolute(flag.split("=", 1)[1]))
        elif flag[:2] in ("-I", "-L") and len(flag) > 2:
            normalized.append(flag[:2] + absolute(flag[2:]))
        else:
            normalized.append(flag)
    return normalized


class CampaignRun:
    def __init__(self, campaign: Campaign, environment: Environment,
                 log_path: str | Path = "CampaignRun.log"):
        self.campaign = campaign
        self.environment = environment
        self.log_path = Path(log_path)
        self.log: list[str] = []
        self.results: list[dict] = []

    def run_campaign(self, args: list[str] | None = None, progress=None):
        """Run every test of the campaign, writing the log and a JSON summary.

        ``args`` overrides the ``.values`` file of each test. ``progress``, when
        given, is called with a dict describing each phase.
        """
        self.log = []
        self.results = []
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.summary_path = self.log_path.with_name(f"{self.log_path.stem}_summary_{timestamp}.json")

        # Identical Oracle/ASPIS builds are shared between tests of the campaign.
        builds: dict[str, tuple[ProgramRunner, dict]] = {}
        try:
            for index, run in enumerate(self.campaign.runs, start=1):
                def report(phase):
                    if progress is not None:
                        progress({"test": run.name, "index": index,
                                  "total": len(self.campaign.runs), "phase": phase,
                                  "sources": ", ".join(run.programs)})

                report("Preparing test")
                self.log.append(f"RUN NAME: {run.name}")
                try:
                    self._run_test(run, args, report, builds)
                except (ValueError, OSError) as error:
                    self.log.append(f"ERROR: CONFIGURATION [{run.name}]: {error}")
                except Exception as error:
                    self.log.append(f"ERROR: {error}")
                self.log.append("")
        finally:
            for runner, _ in builds.values():
                runner.cleanup()
            self.log_path.write_text("\n".join(self.log), encoding="utf-8")
        return self.log

    def _run_test(self, run: RunnerTest, args, report, builds):
        """Build and run Oracle and ASPIS, inject faults, and record the results."""
        sources = source_files(run.programs)
        names = [f.name for f in run.functions] + [v.name for f in run.functions for v in f.variables]
        invalid = [name for name in names if not IDENTIFIER.fullmatch(name)]
        if invalid:
            raise ValueError(f"Invalid debugger symbol name(s): {', '.join(invalid)}")
        try:
            run_args = args if args is not None else args_from_values_file(sources[0])
        except (OSError, UnicodeError) as error:
            raise ValueError(f"Unable to read arguments: {error}") from error
        repetitions = (run.measurement_repetitions or run.execution_repetitions) if run.measure_overhead else 1

        self.log += [
            f"Sources: {', '.join(run.programs)}",
            f"Compiler flags: {shlex.join(run.compiler_flags) if run.compiler_flags else '(none)'}",
            f"Execution repetitions: {run.execution_repetitions}",
            f"Per-process timeout: {run.timeout_ms} ms",
            f"Args: {shlex.join(run_args) if run_args else '(none)'}",
        ]

        def build(label, create_runner, *key):
            key = repr((label, run.programs, run.compiler_flags, run_args,
                        run.functions, repetitions, run.timeout_ms, *key))
            if key in builds:
                report(f"{label} execution loaded from cache")
                return builds[key]
            runner = create_runner()
            report(f"Compiling {label}")
            runner.compile(sources, run.compiler_flags)
            try:
                report(f"{label} execution (measurements) and debugger snapshot")
                builds[key] = runner, runner.execute(run_args, run.functions, repetitions)
            except Exception:
                runner.cleanup()
                raise
            return builds[key]

        _, oracle = build("Oracle", lambda: OracleRunner(self.environment, run.timeout_ms))
        aspis_runner, aspis = build(
            "ASPIS",
            lambda: AspisRunner(self.environment, run.data_protection,
                                run.control_flow_protection, run.timeout_ms),
            run.data_protection, run.control_flow_protection,
        )

        injections = []
        for function in run.functions:
            for injector in create_injectors(
                run.injection, self.environment.gdb_executable, aspis_runner.exe,
                sources[0], function, run_args, run.timeout_ms,
            ):
                results = self._run_injector(injector, run.execution_repetitions, function, report)
                injections += [(result, function) for result in results]

        report("Classifying results")
        differences = measurement_differences(oracle, aspis) if run.measure_overhead else {}
        self.results.append({
            "name": run.name,
            "timeout_ms": run.timeout_ms,
            "oracle": oracle,
            "aspis": aspis,
            "difference": differences,
            "injections": classify_injections(injections, oracle),
        })
        report("Saving analysis")
        atomic_write_text(self.summary_path, json.dumps(self.results, indent=2))
        self.log += comparison_lines(oracle, aspis, differences, run.functions)

    def _run_injector(self, injector, wanted: int, function: TargetFunction, report):
        """Run ``wanted`` injections concurrently.

        Debugger startup/profiling can fail transiently under a highly
        concurrent campaign, so failed attempts are retried (up to ``wanted``
        extra attempts) to always reach the configured sample size.
        """
        results = []
        failures = []

        def attempt(future_or_call):
            try:
                result = future_or_call()
                if result.event == TerminationEvent.INTERNAL_ERROR:
                    detail = result.state.error or result.state.debugger_output[-500:]
                    raise RuntimeError(f"Debugger failed during injection: {detail}")
                results.append(result)
                self.log.append(f"Injection result: {result.event.value} stdout={result.state.stdout[:200]!r}")
            except Exception as error:
                failures.append(error)
            report(f"Fault injection: {function.name} · {len(results)}/{wanted} injections "
                   f"· {len(failures)} failed attempts")

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(injector.run) for _ in range(wanted)]
            for future in concurrent.futures.as_completed(futures):
                attempt(future.result)
        for _ in range(wanted):
            if len(results) >= wanted:
                break
            attempt(injector.run)

        if len(results) != wanted:
            raise RuntimeError(
                f"Injection campaign incomplete: expected {wanted}, got {len(results)}. "
                f"Last error: {failures[-1] if failures else 'unknown error'}"
            )
        if failures:
            self.log.append(f"Recovered {len(failures)} transient injection error(s).")
        return results


def classify_injections(injections, oracle: dict) -> dict:
    """Count the outcome of each ``(result, target_function)`` injection.

    A run that returns normally is an SDC when its output differs from the
    Oracle, latent when only a selected variable differs, masked otherwise.
    """
    summary = {
        "total": len(injections),
        "masked": 0, "sdc": 0, "latent": 0, "timeout": 0,
        "crash": 0, "aspis_data": 0, "aspis_cf": 0,
        "sdc_states": [], "latent_states": [],
    }
    counters = {
        TerminationEvent.TIMEOUT: "timeout",
        TerminationEvent.CRASHED: "crash",
        TerminationEvent.ASPIS_DATA: "aspis_data",
        TerminationEvent.ASPIS_CF: "aspis_cf",
    }
    for result, function in injections:
        state = result.state
        if result.event in counters:
            summary[counters[result.event]] += 1
            continue
        oracle_vars = oracle["snapshots"][function.name]["variables"]
        if state.stdout != oracle["stdout"] or state.stderr != oracle["stderr"]:
            outcome = "sdc"
        elif not state.error and any(
            values_differ(state.variables.get(v.name, ""), oracle_vars.get(v.name, ""))
            for v in function.variables
        ):
            outcome = "latent"
        else:
            outcome = "masked"
        summary[outcome] += 1
        if outcome != "masked":
            summary[f"{outcome}_states"].append({**asdict(state), "function": function.name})
    return summary


def values_differ(injected: str, expected: str) -> bool:
    # Two different addresses are not a corruption: ASPIS changes the layout.
    if injected == expected:
        return False
    return not (HEX_ADDRESS.fullmatch(injected) and HEX_ADDRESS.fullmatch(expected))


def measurement_differences(oracle: dict, aspis: dict) -> dict:
    differences = {}
    for field in ("execution_time_seconds", "binary_size_bytes"):
        delta = aspis[field] - oracle[field]
        differences[field] = {
            "absolute": delta,
            "percent": delta / oracle[field] * 100 if oracle[field] else None,
        }
    return differences


def comparison_lines(oracle: dict, aspis: dict, differences: dict,
                     functions: list[TargetFunction]) -> list[str]:
    lines = ["Comparison:"]
    for field in ("returncode", "stdout", "stderr"):
        status = "OK" if oracle[field] == aspis[field] else "DIFF"
        lines += [f"{field}: {status}", f"  oracle: {oracle[field]!r}", f"  aspis: {aspis[field]!r}"]
    if differences:
        lines += [
            "measurements:",
            f"  measurement_repetitions: oracle={oracle['measurement_repetitions']}, "
            f"aspis={aspis['measurement_repetitions']}",
            f"  execution_time_standard_error_seconds: "
            f"oracle={oracle['execution_time_standard_error_seconds']}, "
            f"aspis={aspis['execution_time_standard_error_seconds']}",
        ]
        for field, unit in (("execution_time_seconds", "s"), ("binary_size_bytes", "bytes")):
            difference = differences[field]
            percent = "n/a" if difference["percent"] is None else f"{difference['percent']:+.2f}%"
            lines.append(
                f"  {field}: oracle={oracle[field]} {unit}, aspis={aspis[field]} {unit}, "
                f"difference={difference['absolute']:+} {unit} ({percent})"
            )
    lines.append("variables:")
    oracle_vars, aspis_vars = oracle["variables"], aspis["variables"]
    selected = [v.name for f in functions for v in f.variables]
    for name in selected or sorted(set(oracle_vars) | set(aspis_vars)):
        expected = oracle_vars.get(name, "<missing>")
        actual = aspis_vars.get(name, "<missing>")
        lines.append(f"  {'OK' if expected == actual else 'DIFF'} {name}: oracle={expected} aspis={actual}")
    return lines
