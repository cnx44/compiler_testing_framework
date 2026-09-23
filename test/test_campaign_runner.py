import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from src.campaign_runner import AspisRunner, CampaignRun, OracleRunner, ProgramRunner
from src.config_parser import (
    Campaign,
    ControlFlowMode,
    DataProtectionMode,
    RunnerTest,
    TargetFunction,
    TargetVariable,
)
from src.constants import DEFAULT_TIMEOUT_MS
from src.env import Environment
from src.models import ExecutionResult, ExecutionState, TerminationEvent


class StubRunner(ProgramRunner):
    def __init__(
        self,
        environment=None,
        debugger=None,
        timeout_ms=DEFAULT_TIMEOUT_MS,
    ):
        super().__init__(environment or Environment(), debugger, timeout_ms)
        self.compiled_sources = []
        self.compiler_flags = []

    def _compile(self, sources, compiler_flags=None):
        self.compiled_sources = sources
        self.compiler_flags = compiler_flags or []
        Path(self.exe).write_bytes(b"binary")


class ProgramRunnerTest(unittest.TestCase):


    @patch("src.campaign_runner.subprocess.run")
    def test_disabled_measurement_runs_once_and_still_collects_state(self, process):
        process.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        runner = StubRunner()
        runner._debug_snapshot = Mock(return_value={"variables": {}, "registers": {}})
        with tempfile.NamedTemporaryFile(suffix=".c") as source:
            try:
                runner.compile([source.name])
                results = runner.execute(measurement_repetitions=9, measure_overhead=False)
            finally:
                runner.cleanup()
        process.assert_called_once()
        runner._debug_snapshot.assert_called_once()
        self.assertEqual(results["measurement_repetitions"], 1)

    def test_is_abstract(self):
        with self.assertRaises(TypeError):
            ProgramRunner(Environment())

    def test_source_files_accepts_c_and_cpp_and_ignores_other_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            c_source = Path(directory) / "main.c"
            cpp_source = Path(directory) / "helper.cpp"
            note = Path(directory) / "note.rs"
            for path in (c_source, cpp_source, note):
                path.touch()

            sources = StubRunner()._source_files(
                [str(c_source), str(note), str(cpp_source)]
            )

        self.assertEqual(sources, [c_source, cpp_source])

    def test_source_files_rejects_missing_or_unsupported_sources(self):
        runner = StubRunner()
        with self.assertRaisesRegex(ValueError, "at least one"):
            runner._source_files(["notes.rs"])
        with self.assertRaisesRegex(FileNotFoundError, "missing.c"):
            runner._source_files(["missing.c"])

    @patch("src.campaign_runner.time.perf_counter", side_effect=[10.0, 10.25])
    @patch("src.campaign_runner.subprocess.run")
    def test_run_compiles_executes_collects_snapshot_and_cleans_up(
        self, run_process, _perf_counter
    ):
        run_process.return_value = subprocess.CompletedProcess(
            args=[], returncode=7, stdout="out", stderr="err"
        )
        runner = StubRunner()
        runner._debug_snapshot = Mock(
            return_value={"variables": {"x": "1"}, "registers": {"rax": "0x1"}}
        )

        with tempfile.NamedTemporaryFile(suffix=".c") as source:
            results = runner.run(
                [source.name], ["one", "two"], compiler_flags=["-lm"]
            )

        self.assertEqual(results["returncode"], 7)
        self.assertEqual(results["stdout"], "out")
        self.assertEqual(results["stderr"], "err")
        self.assertEqual(results["variables"], {"x": "1"})
        self.assertEqual(results["execution_time_seconds"], 0.25)
        self.assertEqual(results["execution_time_samples_seconds"], [0.25])
        self.assertEqual(results["execution_time_stddev_seconds"], 0.0)
        self.assertEqual(results["execution_time_standard_error_seconds"], 0.0)
        self.assertEqual(results["measurement_repetitions"], 1)
        self.assertEqual(results["binary_size_bytes"], 6)
        self.assertEqual(runner.size, 6)
        self.assertEqual(runner.compiled_sources, [Path(source.name)])
        self.assertEqual(runner.compiler_flags, ["-lm"])
        runner._debug_snapshot.assert_called_once_with(
            ["one", "two"], None, "main"
        )
        self.assertEqual(
            results["snapshots"],
            {
                "main": {
                    "variables": {"x": "1"},
                    "registers": {"rax": "0x1"},
                }
            },
        )
        self.assertEqual(run_process.call_args.args[0][1:], ["one", "two"])
        self.assertEqual(run_process.call_args.kwargs["timeout"], 5)
        self.assertIsNone(runner._build_dir)
        self.assertFalse(Path(runner.exe).exists())

    @patch(
        "src.campaign_runner.time.perf_counter",
        side_effect=[0.0, 1.0, 2.0, 5.0],
    )
    @patch("src.campaign_runner.subprocess.run")
    def test_execute_aggregates_repeated_timing_samples(
        self, run_process, _perf_counter
    ):
        run_process.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        runner = StubRunner()
        runner._debug_snapshot = Mock(
            return_value={"variables": {}, "registers": {"rax": "0x0"}}
        )

        with tempfile.NamedTemporaryFile(suffix=".c") as source:
            runner.compile([source.name])
            try:
                results = runner.execute([], [], measurement_repetitions=2)
            finally:
                runner.cleanup()

        self.assertEqual(results["execution_time_samples_seconds"], [1.0, 3.0])
        self.assertEqual(results["execution_time_seconds"], 2.0)
        self.assertAlmostEqual(results["execution_time_stddev_seconds"], 2 ** 0.5)
        self.assertAlmostEqual(
            results["execution_time_standard_error_seconds"], 1.0
        )
        self.assertEqual(results["measurement_repetitions"], 2)
        self.assertEqual(run_process.call_count, 2)

    @patch("src.campaign_runner.subprocess.run")
    def test_execute_uses_configured_timeout(self, run_process):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        runner = StubRunner(timeout_ms=1250)
        runner._debug_snapshot = Mock(
            return_value={"variables": {}, "registers": {}}
        )

        with tempfile.NamedTemporaryFile(suffix=".c") as source:
            runner.compile([source.name])
            try:
                runner.execute()
            finally:
                runner.cleanup()

        self.assertEqual(run_process.call_args.kwargs["timeout"], 1.25)

    @patch("src.campaign_runner.subprocess.run")
    def test_execute_reports_configured_timeout(self, run_process):
        run_process.side_effect = subprocess.TimeoutExpired(["program"], 1.25)
        runner = StubRunner(timeout_ms=1250)

        with tempfile.NamedTemporaryFile(suffix=".c") as source:
            runner.compile([source.name])
            try:
                with self.assertRaisesRegex(
                    RuntimeError, "timed out after 1250 ms"
                ):
                    runner.execute()
            finally:
                runner.cleanup()

    def test_execute_rejects_non_positive_repetitions(self):
        runner = StubRunner()
        with tempfile.NamedTemporaryFile(suffix=".c") as source:
            runner.compile([source.name])
            try:
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    runner.execute(measurement_repetitions=0)
            finally:
                runner.cleanup()

    @patch("src.campaign_runner.subprocess.run")
    def test_execute_collects_a_snapshot_for_each_target_function(
        self, run_process
    ):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        runner = StubRunner()
        runner._debug_snapshot = Mock(
            side_effect=lambda _args, _variables, function: {
                "variables": {function: "value"},
                "registers": {"pc": function},
            }
        )

        with tempfile.NamedTemporaryFile(suffix=".c") as source:
            runner.compile([source.name])
            try:
                results = runner.execute(
                    functions_to_check=["work", "helper", "work"]
                )
            finally:
                runner.cleanup()

        self.assertEqual(list(results["snapshots"]), ["work", "helper"])
        self.assertEqual(
            results["variables"], {"work": "value", "helper": "value"}
        )
        self.assertEqual(
            runner._debug_snapshot.call_args_list,
            [call([], None, "work"), call([], None, "helper")],
        )

    def test_parses_gdb_snapshot_formats(self):
        runner = StubRunner()
        output = """noise
@@ARGS@@
argc = 3
(char **) argv = 0x1234
invalid name = ignored
@@LOCALS@@
(int) total = 9
@@REGISTERS@@
rax 0x1
rbx = 0x2
"""

        variables = {
            **runner._parse_debug_variables(output, "@@ARGS@@", "@@LOCALS@@"),
            **runner._parse_debug_variables(output, "@@LOCALS@@", "@@REGISTERS@@"),
        }

        self.assertEqual(
            variables,
            {"argc": "3", "argv": "0x1234", "total": "9"},
        )
        self.assertEqual(
            runner._parse_debug_registers(output),
            {"rax": "0x1", "rbx": "0x2"},
        )



    def test_debug_snapshot_collects_requested_global_variables(self):
        runner = StubRunner()
        runner._debuggers = Mock(return_value=["gdb"])
        runner._run_debugger = Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                """@@ARGS@@
No arguments.
@@LOCALS@@
No locals.
@@GLOBALS@@
sum = 2
@@REGISTERS@@
rax = 0x2
""",
                "",
            )
        )

        snapshot = runner._debug_snapshot([], ["sum"])

        self.assertEqual(snapshot["variables"], {"sum": "2"})
        self.assertEqual(snapshot["registers"], {"rax": "0x2"})
        runner._run_debugger.assert_called_once_with("gdb", "main", [], ["sum"])



    @patch("src.campaign_runner.subprocess.run")
    def test_gdb_command_prints_requested_global_variables(self, run_process):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        runner = StubRunner()
        runner.exe = "/tmp/program"
        runner.src = "main.c"

        runner._run_debugger("gdb", "work", [], ["sum"])

        kwargs = run_process.call_args.kwargs
        self.assertIn("env", kwargs)
        self.assertEqual(kwargs["env"]["VARS_TO_CHECK"], "sum")
        self.assertEqual(kwargs["env"]["TARGET_FUNC"], "work")
        self.assertEqual(kwargs["timeout"], 5)



    def test_debugger_rejects_invalid_variable_names(self):
        with self.assertRaisesRegex(ValueError, "bad-name"):
            StubRunner()._run_debugger("gdb", "main", [], ["bad-name"])

    def test_debugger_rejects_invalid_function_names(self):
        with self.assertRaisesRegex(ValueError, "bad-name"):
            StubRunner()._run_debugger("gdb", "bad-name", [])

    def test_debug_snapshot_reports_when_no_debugger_is_available(self):
        runner = StubRunner()
        runner._debuggers = Mock(return_value=[])
        with self.assertRaisesRegex(RuntimeError, "gdb is not installed"):
            runner._debug_snapshot([])

    def test_debugger_command_rejects_unknown_debugger(self):
        with self.assertRaisesRegex(ValueError, "Unsupported debugger"):
            StubRunner()._run_debugger("unknown", "main", [])

    @patch("src.campaign_runner.subprocess.run")
    def test_debugger_command_accepts_custom_debugger_paths(self, run_process):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        runner = StubRunner()
        runner.exe = "/tmp/program"
        runner.src = "main.c"
        
        runner._run_debugger("/opt/custom/gdb-multiarch", "main", [])
        command = run_process.call_args.args[0]
        self.assertEqual(command[0], "/opt/custom/gdb-multiarch")


class OracleRunnerTest(unittest.TestCase):
    @patch("src.campaign_runner.subprocess.run")
    def test_compile_uses_compiler_from_environment_and_all_sources(self, run_process):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        runner = OracleRunner(Environment(vanilla_compiler="/tools/clang"))
        runner.exe = "/tmp/output"
        sources = [Path("main.c"), Path("helper.cpp")]

        runner._compile(sources, ["-Wall", "-lm"])

        run_process.assert_called_once_with(
            [
                "/tools/clang",
                "-g",
                "-O0",
                "main.c",
                "helper.cpp",
                "-Wall",
                "-lm",
                "-o",
                "/tmp/output",
            ],
            capture_output=True,
            text=True,
        )

    def test_compile_requires_a_configured_compiler(self):
        runner = OracleRunner(Environment(vanilla_compiler=""))
        with self.assertRaisesRegex(RuntimeError, "vanilla_compiler"):
            runner._compile([Path("main.c")])

    @patch("src.campaign_runner.subprocess.run")
    def test_compile_exposes_compiler_errors(self, run_process):
        run_process.return_value = subprocess.CompletedProcess([], 1, "", "bad C")
        runner = OracleRunner(Environment(vanilla_compiler="clang"))
        runner.exe = "/tmp/output"
        with self.assertRaisesRegex(RuntimeError, "bad C"):
            runner._compile([Path("main.c")])


class AspisRunnerTest(unittest.TestCase):
    def test_mode_arguments_cover_every_supported_combination(self):
        environment = Environment()
        data_flags = {
            DataProtectionMode.EDDI: "--eddi",
            DataProtectionMode.SEDDI: "--seddi",
            DataProtectionMode.FDSC: "--fdsc",
            DataProtectionMode.NONE: "--no-dup",
        }
        control_flags = {
            ControlFlowMode.CFCSS: "--cfcss",
            ControlFlowMode.RASM: "--rasm",
            ControlFlowMode.INTER_RASM: "--inter-rasm",
            ControlFlowMode.NONE: "--no-cfc",
        }
        for data_mode, data_flag in data_flags.items():
            for control_mode, control_flag in control_flags.items():
                with self.subTest(data=data_mode, control=control_mode):
                    runner = AspisRunner(environment, data_mode, control_mode)
                    self.assertEqual(
                        runner._aspis_mode_args(),
                        [data_flag, control_flag],
                    )

    @patch("src.campaign_runner.subprocess.run")
    def test_compile_uses_all_environment_paths_modes_and_sources(self, run_process):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            aspis_script = Path(directory) / "aspis.sh"
            aspis_script.touch()
            environment = Environment(
                aspis_path=str(aspis_script),
                vanilla_compiler="unused",
                llvm_path="/tools/llvm",
                aspis_flags=["--no-cleanup", "--coarse-grained"],
            )
            runner = AspisRunner(
                environment,
                DataProtectionMode.SEDDI,
                ControlFlowMode.INTER_RASM,
            )
            runner.exe = str(Path(directory) / "program")

            runner._compile(
                [Path("main.c"), Path("helper.cpp")], ["-Wall", "-lm"]
            )

        run_process.assert_called_once_with(
            [
                str(aspis_script.resolve()),
                "-g",
                "-o",
                "program",
                "--build-dir",
                directory,
                "--llvm-bin",
                "/tools/llvm",
                "--seddi",
                "--inter-rasm",
                "--no-cleanup",
                "--coarse-grained",
                str(Path("main.c").resolve()),
                str(Path("helper.cpp").resolve()),
                str(
                    (Path(__file__).parents[1] / "src" / "aspis_handlers.c").resolve()
                ),
                "-Wall",
                "-lm",
            ],
            capture_output=True,
            text=True,
            cwd=Path(directory),
        )

    def test_compiler_paths_remain_relative_to_campaign_directory(self):
        runner = AspisRunner(Environment())

        flags = runner._compiler_flags_for_build_dir([
            "-Iinclude",
            "-L",
            "lib",
            "-include",
            "config.h",
            "--sysroot=sysroot",
            "-lm",
        ])

        self.assertEqual(
            flags,
            [
                f"-I{Path('include').resolve()}",
                "-L",
                str(Path("lib").resolve()),
                "-include",
                str(Path("config.h").resolve()),
                f"--sysroot={Path('sysroot').resolve()}",
                "-lm",
            ],
        )

    @patch("src.campaign_runner.subprocess.run")
    def test_compile_omits_llvm_option_when_not_configured(self, run_process):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            aspis_script = Path(directory) / "aspis.sh"
            aspis_script.touch()
            runner = AspisRunner(Environment(aspis_path=str(aspis_script)))
            runner.exe = str(Path(directory) / "program")
            runner._compile([Path("main.c")])

        command = run_process.call_args.args[0]
        self.assertNotIn("--llvm-bin", command)

    def test_compile_requires_existing_aspis_script(self):
        runner = AspisRunner(Environment(aspis_path="/missing/aspis.sh"))
        with self.assertRaisesRegex(RuntimeError, "aspis_path"):
            runner._compile([Path("main.c")])

    @patch("src.campaign_runner.subprocess.run")
    def test_compile_exposes_stdout_and_stderr_on_failure(self, run_process):
        run_process.return_value = subprocess.CompletedProcess(
            [], 1, "ASPIS output", "ASPIS error"
        )
        with tempfile.NamedTemporaryFile(suffix=".sh") as script:
            runner = AspisRunner(Environment(aspis_path=script.name))
            runner.exe = "/tmp/program"
            with self.assertRaisesRegex(
                RuntimeError, "ASPIS output\nASPIS error"
            ):
                runner._compile([Path("main.c")])


class CampaignRunTest(unittest.TestCase):
    def _campaign(
        self,
        programs=None,
        compiler_flags=None,
        variables=None,
        execution_repetitions=1,
        functions=None,
        timeout_ms=DEFAULT_TIMEOUT_MS,
    ):
        target_funcs = [TargetFunction(f, [TargetVariable(v) for v in (variables or ["x"])]) for f in (functions or ["main"])]
        return Campaign(
            runs=[
                RunnerTest(
                    name="sample",
                    programs=programs or ["main.c", "helper.cpp"],
                    data_protection=DataProtectionMode.FDSC,
                    control_flow_protection=ControlFlowMode.RASM,
                    compiler_flags=compiler_flags or [],
                    execution_repetitions=execution_repetitions,
                    functions=target_funcs,
                    timeout_ms=timeout_ms,
                )
            ]
        )

    @patch("src.campaign_runner.BaseInjector.create_for_target", return_value=[])
    @patch("src.campaign_runner.AspisRunner")
    @patch("src.campaign_runner.OracleRunner")
    def test_runs_both_variants_with_same_programs_and_arguments(
        self, oracle_class, aspis_class, create_injectors
    ):
        oracle_results = {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "variables": {"x": "1"},
            "execution_time_seconds": 1.0,
            "binary_size_bytes": 100,
        }
        aspis_results = dict(oracle_results)
        order = []
        oracle_class.return_value.compile.side_effect = (
            lambda programs, flags: order.append("oracle compile")
        )
        aspis_class.return_value.compile.side_effect = (
            lambda programs, flags: order.append("aspis compile")
        )
        oracle_class.return_value.execute.side_effect = (
            lambda args, variables, repetitions, functions, **kwargs: (
                order.append("oracle execute") or oracle_results
            )
        )
        aspis_class.return_value.execute.side_effect = (
            lambda args, variables, repetitions, functions, **kwargs: (
                order.append("aspis execute") or aspis_results
            )
        )
        environment = Environment(aspis_path="/aspis", vanilla_compiler="/clang")

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "campaign.log"
            campaign_run = CampaignRun(
                self._campaign(
                    compiler_flags=["-lm"],
                    execution_repetitions=5,
                    timeout_ms=1250,
                ),
                environment,
                str(log_path),
                debugger="gdb",
            )
            log = campaign_run.run_campaign(["a", "b"])
            written_log = log_path.read_text(encoding="utf-8")

        oracle_class.assert_called_once_with(
            environment, "gdb", timeout_ms=1250
        )
        oracle_class.return_value.compile.assert_called_once_with(
            ["main.c", "helper.cpp"], ["-lm"]
        )
        oracle_class.return_value.execute.assert_called_once_with(
            ["a", "b"], ["x"], 5, ["main"], measure_overhead=True
        )
        aspis_class.assert_called_once_with(
            environment,
            data_protection=DataProtectionMode.FDSC,
            control_flow_protection=ControlFlowMode.RASM,
            debugger="gdb",
            timeout_ms=1250,
        )
        aspis_class.return_value.compile.assert_called_once_with(
            ["main.c", "helper.cpp"], ["-lm"]
        )
        aspis_class.return_value.execute.assert_called_once_with(
            ["a", "b"], ["x"], 5, ["main"], measure_overhead=True
        )
        self.assertEqual(
            order,
            ["oracle compile", "oracle execute", "aspis compile", "aspis execute"],
        )
        self.assertIn("returncode: OK", log)
        self.assertIn("Compiler flags: -lm", log)
        self.assertIn("Execution repetitions: 5", log)
        self.assertIn("Per-process timeout: 1250 ms", log)
        self.assertIn("  OK x: oracle=1 aspis=1", log)
        self.assertEqual(
            campaign_run.results[0]["difference"]["binary_size_bytes"],
            {"absolute": 0, "percent": 0.0},
        )
        self.assertEqual(campaign_run.results[0]["timeout_ms"], 1250)
        self.assertEqual(written_log, "\n".join(log))

    @patch("src.injector.RegisterInjector")
    @patch("src.campaign_runner.AspisRunner")
    @patch("src.campaign_runner.OracleRunner")
    def test_runs_injector_and_logs_execution_results(
        self, oracle_class, aspis_class, injector_class
    ):
        normal_results = {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "variables": {},
            "execution_time_seconds": 1.0,
            "binary_size_bytes": 100,
        }
        oracle_class.return_value.execute.return_value = normal_results
        aspis_class.return_value.execute.return_value = normal_results
        aspis_class.return_value.exe = "/tmp/aspis-program"
        execution_result = ExecutionResult(
            TerminationEvent.RETURNED,
            ExecutionState(stdout="injected run"),
        )
        injector_class.return_value.run.return_value = execution_result

        with tempfile.TemporaryDirectory() as directory:
            campaign_run = CampaignRun(
                self._campaign(
                    functions=["work"],
                    execution_repetitions=2,
                    timeout_ms=1250,
                ),
                Environment(),
                Path(directory) / "campaign.log",
            )
            log = campaign_run.run_campaign(["arg"])

        injector_class.assert_called_once_with(
            campaign_run.environment,
            "/tmp/aspis-program",
            "main.c",
            "work",
            data_only=False,
            debugger=None,
            timeout_ms=1250,
        )
        self.assertEqual(
            injector_class.return_value.run.call_args_list,
            [call(["arg"]), call(["arg"])],
        )
        self.assertEqual(
            log.count(f"Injection result: {execution_result}"),
            2,
        )

    @patch("src.injector.RegisterInjector")
    @patch("src.campaign_runner.AspisRunner")
    @patch("src.campaign_runner.OracleRunner")
    def test_runs_injector_concurrently(
        self, oracle_class, aspis_class, injector_class
    ):
        normal_results = {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "variables": {},
            "execution_time_seconds": 1.0,
            "binary_size_bytes": 100,
        }
        oracle_class.return_value.execute.return_value = normal_results
        aspis_class.return_value.execute.return_value = normal_results
        
        # We'll run 4 injections and make them wait for each other at a barrier.
        # If they aren't running concurrently, the barrier will timeout/deadlock,
        # but since tests have timeouts, we use a threading.Barrier with a small timeout.
        concurrency_level = 4
        barrier = threading.Barrier(concurrency_level, timeout=5.0)
        
        def mock_run(args):
            barrier.wait()
            return ExecutionResult(TerminationEvent.RETURNED, ExecutionState(stdout="injected run"))

        injector_class.return_value.run.side_effect = mock_run

        with tempfile.TemporaryDirectory() as directory:
            campaign_run = CampaignRun(
                self._campaign(functions=["work"], execution_repetitions=concurrency_level),
                Environment(),
                Path(directory) / "campaign.log",
            )
            campaign_run.run_campaign(["arg"])
            
        # If we reached here without a BrokenBarrierError or deadlock, concurrency works!
        self.assertEqual(injector_class.return_value.run.call_count, concurrency_level)

    @patch("src.campaign_runner.AspisRunner")
    @patch("src.campaign_runner.OracleRunner")
    def test_reads_arguments_from_values_file(self, oracle_class, aspis_class):
        results = {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "variables": {},
            "execution_time_seconds": 1.0,
            "binary_size_bytes": 100,
        }
        oracle_class.return_value.execute.return_value = results
        aspis_class.return_value.execute.return_value = results

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.c"
            source.touch()
            source.with_suffix(".values").write_text(
                "args[2] = second\nargs[1] = first\nx = ignored\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "campaign.log"
            campaign_run = CampaignRun(
                self._campaign(programs=[str(source)]),
                Environment(),
                str(log_path),
            )
            campaign_run.run_campaign()

        oracle_class.return_value.compile.assert_called_once_with([str(source)], [])
        oracle_class.return_value.execute.assert_called_once_with(
            ["first", "second"], ["x"], 1, ["main"], measure_overhead=True
        )

    def test_skips_run_without_supported_entry_source(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = CampaignRun(
                self._campaign(programs=["README.txt"]),
                Environment(),
                str(Path(directory) / "campaign.log"),
            )
            log = runner.run_campaign()
        self.assertTrue(any("ERROR: CONFIGURATION" in line and "No C or C++ source file" in line for line in log))

    @patch("src.campaign_runner.OracleRunner")
    def test_records_runner_errors_and_continues(self, oracle_class):
        oracle_class.return_value.compile.side_effect = RuntimeError("compiler down")
        with tempfile.TemporaryDirectory() as directory:
            runner = CampaignRun(
                self._campaign(),
                Environment(),
                str(Path(directory) / "campaign.log"),
            )
            log = runner.run_campaign([])
        self.assertIn("ERROR: compiler down", log)

    def test_comparison_marks_differences_and_missing_variables(self):
        runner = CampaignRun(self._campaign(), Environment())
        lines = runner._comparison_lines(
            {
                "returncode": 0,
                "stdout": "expected",
                "stderr": "",
                "variables": {"x": "1"},
                "execution_time_seconds": 1.0,
                "binary_size_bytes": 100,
            },
            {
                "returncode": 1,
                "stdout": "actual",
                "stderr": "",
                "variables": {},
                "execution_time_seconds": 1.5,
                "binary_size_bytes": 125,
            },
            ["x"],
        )
        self.assertIn("returncode: DIFF", lines)
        self.assertIn("stdout: DIFF", lines)
        self.assertIn("stderr: OK", lines)
        self.assertIn(
            "  binary_size_bytes: oracle=100 bytes, aspis=125 bytes, difference=+25 bytes (+25.00%)",
            lines,
        )
        self.assertIn("  DIFF x: oracle=1 aspis=<missing>", lines)


if __name__ == "__main__":
    unittest.main()
