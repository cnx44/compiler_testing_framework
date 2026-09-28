import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from src.campaign_runner import (
    AspisRunner,
    CampaignRun,
    OracleRunner,
    ProgramRunner,
    absolute_flag_paths,
    args_from_values_file,
    classify_injections,
    comparison_lines,
    source_files,
)
from src.config_parser import (
    Campaign,
    ControlFlowMode,
    DataProtectionMode,
    RunnerTest,
    TargetFunction,
    TargetVariable,
)
from src.env import Environment
from src.models import ExecutionResult, ExecutionState, TerminationEvent

HANDLERS = str(Path(__file__).parents[1] / "src" / "aspis_handlers.c")
DUMP = "@@ARGS@@\nn = 3\n@@LOCALS@@\nx = 7\nbuf = {1, 2}\n@@REGISTERS@@\nrax            0x7    7\n"


class StubRunner(ProgramRunner):
    def _compile(self, sources, compiler_flags):
        self.compiled = (sources, compiler_flags)
        Path(self.exe).write_bytes(b"binary")


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ProgramRunnerTest(unittest.TestCase):
    def setUp(self):
        self.runner = StubRunner(Environment(), timeout_ms=1250)
        self.addCleanup(self.runner.cleanup)

    def test_is_abstract(self):
        with self.assertRaises(TypeError):
            ProgramRunner(Environment())

    def test_compile_keeps_binary_until_cleanup(self):
        self.runner.compile([Path("main.c")], ["-lm"])
        self.assertEqual(self.runner.compiled, ([Path("main.c")], ["-lm"]))
        self.assertEqual(self.runner.size, 6)
        exe = Path(self.runner.exe)
        self.assertTrue(exe.is_file())
        self.runner.cleanup()
        self.assertFalse(exe.exists())

    def test_execute_requires_compiled_binary(self):
        with self.assertRaisesRegex(RuntimeError, "compiled"):
            self.runner.execute([], [])

    @patch("src.campaign_runner.time.perf_counter", side_effect=[0.0, 1.0, 2.0, 5.0])
    @patch("src.campaign_runner.subprocess.run", return_value=completed(7, "out\n", "err"))
    def test_execute_aggregates_timing_and_snapshots_each_function(self, run, _clock):
        self.runner.compile([Path("main.c")])
        snapshots = {"main": {"variables": {"x": "1"}, "registers": {"rax": "1"}},
                     "work": {"variables": {"y": "2"}, "registers": {"rax": "2"}}}
        self.runner.snapshot = Mock(side_effect=lambda args, f: snapshots[f.name])
        functions = [TargetFunction("main"), TargetFunction("work")]

        results = self.runner.execute(["a"], functions, repetitions=2)

        self.assertEqual(run.call_args.args[0], [self.runner.exe, "a"])
        self.assertEqual(run.call_args.kwargs["timeout"], 1.25)
        self.assertEqual(results["returncode"], 7)
        self.assertEqual(results["stdout"], "out")
        self.assertEqual(results["execution_time_samples_seconds"], [1.0, 3.0])
        self.assertEqual(results["execution_time_seconds"], 2.0)
        self.assertAlmostEqual(results["execution_time_stddev_seconds"], 2 ** 0.5)
        self.assertAlmostEqual(results["execution_time_standard_error_seconds"], 1.0)
        self.assertEqual(results["measurement_repetitions"], 2)
        self.assertEqual(results["binary_size_bytes"], 6)
        self.assertEqual(results["snapshots"], snapshots)
        self.assertEqual(results["variables"], {"x": "1", "y": "2"})

    @patch("src.campaign_runner.subprocess.run",
           side_effect=subprocess.TimeoutExpired("program", 1.25))
    def test_execute_reports_configured_timeout(self, _run):
        self.runner.compile([Path("main.c")])
        with self.assertRaisesRegex(RuntimeError, "1250 ms"):
            self.runner.execute([], [])

    @patch("src.campaign_runner.shutil.which", return_value="/usr/bin/gdb")
    @patch("src.campaign_runner.subprocess.run", return_value=completed(stdout="noise\n" + DUMP))
    def test_snapshot_runs_gdb_script_with_requested_variables(self, run, _which):
        self.runner.exe = "/tmp/program"
        function = TargetFunction("work", [TargetVariable("buf", 2), TargetVariable("G", is_global=True)])

        snapshot = self.runner.snapshot(["a"], function)

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["gdb", "-nx", "--batch", "-q"])
        self.assertTrue(command[5].endswith("snapshot_gdb.py"))
        self.assertEqual(command[6:], ["--args", "/tmp/program", "a"])
        self.assertEqual(run.call_args.kwargs["env"]["TARGET_FUNC"], "work")
        self.assertEqual(run.call_args.kwargs["env"]["DUMP_VARS"], "buf:2,G")
        self.assertEqual(snapshot, {"variables": {"n": "3", "x": "7", "buf": "{1, 2}"},
                                    "registers": {"rax": "0x7"}})

    @patch("src.campaign_runner.shutil.which", return_value="/usr/bin/gdb")
    @patch("src.campaign_runner.subprocess.run", return_value=completed(stdout="No symbol main"))
    def test_snapshot_without_dump_fails(self, _run, _which):
        with self.assertRaisesRegex(RuntimeError, "snapshot of main failed"):
            self.runner.snapshot([], TargetFunction("main"))

    @patch("src.campaign_runner.shutil.which", return_value=None)
    def test_snapshot_requires_gdb(self, _which):
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            self.runner.snapshot([], TargetFunction("main"))


class OracleRunnerTest(unittest.TestCase):
    @patch("src.campaign_runner.subprocess.run", return_value=completed())
    def test_compiles_all_sources_with_debug_info(self, run):
        runner = OracleRunner(Environment(vanilla_compiler="/usr/bin/clang"))
        runner.exe = "/tmp/program"
        runner._compile([Path("main.c"), Path("helper.cpp")], ["-lm"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/clang", "-g", "-O0", "main.c",
                                                 "helper.cpp", "-lm", "-o", "/tmp/program"])

    def test_requires_a_configured_compiler(self):
        with self.assertRaisesRegex(RuntimeError, "vanilla_compiler"):
            OracleRunner(Environment(vanilla_compiler=""))._compile([Path("main.c")], [])

    @patch("src.campaign_runner.subprocess.run", return_value=completed(1, "", "syntax error"))
    def test_exposes_compiler_errors(self, _run):
        with self.assertRaisesRegex(RuntimeError, "syntax error"):
            OracleRunner(Environment())._compile([Path("main.c")], [])


class AspisRunnerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.script = self.directory / "aspis.sh"
        self.script.touch()

    def compile(self, environment, *modes, flags=()):
        runner = AspisRunner(environment, *modes)
        runner.exe = str(self.directory / "program")
        runner._compile([Path("main.c"), Path("helper.cpp")], list(flags))

    @patch("src.campaign_runner.subprocess.run", return_value=completed())
    def test_command_uses_environment_modes_sources_and_handlers(self, run):
        environment = Environment(aspis_path=str(self.script), llvm_path="/llvm",
                                  aspis_flags=["--no-cleanup"])
        self.compile(environment, DataProtectionMode.SEDDI, ControlFlowMode.INTER_RASM,
                     flags=["-Iinclude", "-lm"])
        run.assert_called_once_with(
            [str(self.script.resolve()), "-g", "-o", "program", "--build-dir", str(self.directory),
             "--llvm-bin", "/llvm", "--seddi", "--inter-rasm", "--no-cleanup",
             str(Path("main.c").resolve()), str(Path("helper.cpp").resolve()), HANDLERS,
             f"-I{Path('include').resolve()}", "-lm"],
            capture_output=True, text=True, cwd=self.directory,
        )

    @patch("src.campaign_runner.subprocess.run", return_value=completed())
    def test_every_mode_has_an_aspis_flag(self, run):
        environment = Environment(aspis_path=str(self.script))
        for data in DataProtectionMode:
            for control in ControlFlowMode:
                self.compile(environment, data, control)
                command = run.call_args.args[0]
                self.assertNotIn("--llvm-bin", command)
                self.assertEqual(len([arg for arg in command if arg.startswith("--")
                                      and arg != "--build-dir"]), 2)

    def test_requires_existing_aspis_script(self):
        with self.assertRaisesRegex(RuntimeError, "aspis_path"):
            self.compile(Environment(aspis_path="/missing/aspis.sh"))

    @patch("src.campaign_runner.subprocess.run", return_value=completed(1, "ASPIS output", "ASPIS error"))
    def test_exposes_stdout_and_stderr_on_failure(self, _run):
        with self.assertRaisesRegex(RuntimeError, "ASPIS output\nASPIS error"):
            self.compile(Environment(aspis_path=str(self.script)))

    def test_compiler_paths_are_made_absolute(self):
        self.assertEqual(
            absolute_flag_paths(["-Iinclude", "-L", "lib", "-include", "config.h",
                                 "--sysroot=sysroot", "-lm", "-I"]),
            [f"-I{Path('include').resolve()}", "-L", str(Path("lib").resolve()),
             "-include", str(Path("config.h").resolve()),
             f"--sysroot={Path('sysroot').resolve()}", "-lm", "-I"],
        )


class HelpersTest(unittest.TestCase):
    def test_source_files_require_existing_c_or_cpp(self):
        with tempfile.TemporaryDirectory() as directory:
            c_file = Path(directory, "main.c")
            cpp_file = Path(directory, "helper.cpp")
            c_file.touch()
            cpp_file.touch()
            self.assertEqual(source_files([str(c_file), str(cpp_file)]), [c_file, cpp_file])
            with self.assertRaisesRegex(ValueError, "Only C and C"):
                source_files([str(c_file), "lib.rs"])
            with self.assertRaises(FileNotFoundError):
                source_files([str(Path(directory, "missing.c"))])

    def test_arguments_are_read_in_index_order(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "main.c")
            self.assertEqual(args_from_values_file(source), [])
            source.with_suffix(".values").write_text("args[2] = second\nargs[1] = first\nx = 1\n")
            self.assertEqual(args_from_values_file(source), ["first", "second"])

    def test_comparison_marks_differences_and_missing_variables(self):
        oracle = {"returncode": 0, "stdout": "expected", "stderr": "", "variables": {"x": "1"},
                  "execution_time_seconds": 1.0, "binary_size_bytes": 100,
                  "measurement_repetitions": 1, "execution_time_standard_error_seconds": 0.0}
        aspis = {**oracle, "returncode": 1, "stdout": "actual", "variables": {},
                 "execution_time_seconds": 1.5, "binary_size_bytes": 125}
        differences = {"execution_time_seconds": {"absolute": 0.5, "percent": 50.0},
                       "binary_size_bytes": {"absolute": 25, "percent": 25.0}}
        lines = comparison_lines(oracle, aspis, differences, [TargetFunction("main", [TargetVariable("x")])])
        self.assertIn("returncode: DIFF", lines)
        self.assertIn("stdout: DIFF", lines)
        self.assertIn("stderr: OK", lines)
        self.assertIn("  binary_size_bytes: oracle=100 bytes, aspis=125 bytes, difference=+25 bytes (+25.00%)", lines)
        self.assertIn("  DIFF x: oracle=1 aspis=<missing>", lines)

    def test_classification(self):
        function = TargetFunction("main", [TargetVariable("x"), TargetVariable("p")])
        oracle = {"stdout": "ok", "stderr": "",
                  "snapshots": {"main": {"variables": {"x": "1", "p": "0x10"}}}}

        def returned(stdout="ok", error="", **variables):
            state = ExecutionState(stdout=stdout, variables={"x": "1", "p": "0x10", **variables}, error=error)
            return ExecutionResult(TerminationEvent.RETURNED, state), function

        injections = [
            returned(),                        # identical
            returned(p="0x20"),                # another address is not a corruption
            returned(x="2"),                   # latent
            returned(x="2", error="no dump"),  # state unknown: not latent
            returned(stdout="bad"),            # sdc
            (ExecutionResult(TerminationEvent.CRASHED, ExecutionState()), function),
            (ExecutionResult(TerminationEvent.TIMEOUT, ExecutionState()), function),
            (ExecutionResult(TerminationEvent.ASPIS_DATA, ExecutionState()), function),
            (ExecutionResult(TerminationEvent.ASPIS_CF, ExecutionState()), function),
        ]
        summary = classify_injections(injections, oracle)
        counts = {k: v for k, v in summary.items() if not k.endswith("_states")}
        self.assertEqual(counts, {"total": 9, "masked": 3, "sdc": 1, "latent": 1, "timeout": 1,
                                  "crash": 1, "aspis_data": 1, "aspis_cf": 1})
        self.assertEqual(summary["sdc_states"][0]["stdout"], "bad")
        self.assertEqual(summary["latent_states"][0]["function"], "main")


class CampaignRunTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "main.c"
        self.source.touch()
        self.case = RunnerTest("test", [str(self.source)], DataProtectionMode.FDSC,
                               ControlFlowMode.RASM, timeout_ms=1250,
                               functions=[TargetFunction("main", [TargetVariable("x")])])
        self.results = {"returncode": 0, "stdout": "ok", "stderr": "", "variables": {"x": "1"},
                        "snapshots": {"main": {"variables": {"x": "1"}}},
                        "execution_time_seconds": 1.0, "binary_size_bytes": 100,
                        "measurement_repetitions": 1, "execution_time_standard_error_seconds": 0.0}
        self.injectors = []
        for target, value in (("OracleRunner", None), ("AspisRunner", None),
                              ("create_injectors", self.create_injectors)):
            patcher = patch(f"src.campaign_runner.{target}",
                            side_effect=value or (lambda *a, **k: self.mock_runner()))
            self.addCleanup(patcher.stop)
            setattr(self, target, patcher.start())

    def create_injectors(self, *args, **kwargs):
        return self.injectors.pop(0) if self.injectors else []

    def mock_runner(self):
        runner = Mock(exe="/tmp/aspis-program")
        runner.execute.return_value = self.results
        return runner

    def injector(self, *results):
        injector = Mock()
        injector.run.side_effect = list(results)
        return injector

    def run_campaign(self, runs, args=None):
        campaign_run = CampaignRun(Campaign(runs), Environment(), self.root / "campaign.log")
        campaign_run.run_campaign(args)
        return campaign_run

    def test_builds_both_variants_and_writes_log_and_summary(self):
        run = replace(self.case, compiler_flags=["-lm"], execution_repetitions=5)
        campaign_run = self.run_campaign([run], ["a", "b"])

        self.OracleRunner.assert_called_once_with(campaign_run.environment, 1250)
        self.AspisRunner.assert_called_once_with(campaign_run.environment, DataProtectionMode.FDSC,
                                                 ControlFlowMode.RASM, 1250)
        self.create_injectors.assert_called_once()
        self.assertEqual(self.create_injectors.call_args.args[5:], (["a", "b"], 1250))
        log = campaign_run.log
        for line in ("Compiler flags: -lm", "Execution repetitions: 5", "Per-process timeout: 1250 ms",
                     "Args: a b", "returncode: OK", "  OK x: oracle=1 aspis=1"):
            self.assertIn(line, log)
        self.assertEqual((self.root / "campaign.log").read_text(), "\n".join(log))
        self.assertEqual(campaign_run.results[0]["difference"]["binary_size_bytes"],
                         {"absolute": 0, "percent": 0.0})
        self.assertTrue(campaign_run.summary_path.is_file())

    def test_measurement_repetitions(self):
        runner = self.mock_runner()
        self.OracleRunner.side_effect = None
        self.OracleRunner.return_value = runner
        self.run_campaign([replace(self.case, execution_repetitions=2, measurement_repetitions=7)])
        self.run_campaign([replace(self.case, execution_repetitions=3)])
        self.run_campaign([replace(self.case, execution_repetitions=3, measure_overhead=False)])
        self.assertEqual([c.args[2] for c in runner.execute.call_args_list], [7, 3, 1])

    def test_identical_builds_are_shared(self):
        runs = [self.case, replace(self.case, name="reps", execution_repetitions=2),
                replace(self.case, name="timeout", timeout_ms=100),
                replace(self.case, name="modes", data_protection=DataProtectionMode.EDDI),
                replace(self.case, name="reuse")]
        campaign_run = self.run_campaign(runs)
        self.assertEqual(len(campaign_run.results), 5)
        self.assertEqual(self.OracleRunner.call_count, 3)
        self.assertEqual(self.AspisRunner.call_count, 4)

    def test_failed_snapshot_releases_runner_and_does_not_poison_cache(self):
        failed, success = self.mock_runner(), self.mock_runner()
        failed.execute.side_effect = RuntimeError("snapshot failure")
        self.OracleRunner.side_effect = [failed, success]
        campaign_run = self.run_campaign([self.case, replace(self.case, name="retry")])
        failed.cleanup.assert_called_once()
        success.cleanup.assert_called_once()
        self.assertEqual([r["name"] for r in campaign_run.results], ["retry"])
        self.assertIn("ERROR: snapshot failure", campaign_run.log)

    def test_configuration_errors_do_not_abort_other_tests(self):
        other = self.root / "other.c"
        other.touch()
        self.source.with_suffix(".values").write_bytes(b"\xff")
        runs = [self.case, replace(self.case, name="rust", programs=["main.rs"]),
                replace(self.case, name="bad", functions=[TargetFunction("1st")]),
                replace(self.case, name="second", programs=[str(other)])]
        campaign_run = self.run_campaign(runs)
        self.assertEqual([r["name"] for r in campaign_run.results], ["second"])
        errors = [line for line in campaign_run.log if line.startswith("ERROR: CONFIGURATION")]
        self.assertEqual(len(errors), 3)
        self.assertIn("Unable to read arguments", errors[0])

    def test_classification_uses_each_functions_own_snapshot(self):
        functions = [TargetFunction(name, [TargetVariable("x")]) for name in ("first", "second")]
        self.results["snapshots"] = {"first": {"variables": {"x": "1"}},
                                     "second": {"variables": {"x": "2"}}}
        for value in ("1", "2"):
            state = ExecutionState(stdout="ok", variables={"x": value})
            self.injectors.append([self.injector(ExecutionResult(TerminationEvent.RETURNED, state))])
        campaign_run = self.run_campaign([replace(self.case, functions=functions)])
        self.assertEqual(campaign_run.results[0]["injections"]["masked"], 2)

    def test_debugger_failures_are_retried(self):
        failure = ExecutionResult(TerminationEvent.INTERNAL_ERROR, ExecutionState(error="startup failed"))
        success = ExecutionResult(TerminationEvent.RETURNED, ExecutionState(stdout="ok", variables={"x": "1"}))
        self.injectors.append([self.injector(failure, RuntimeError("profiling"), success, success)])
        campaign_run = self.run_campaign([replace(self.case, execution_repetitions=2)])
        self.assertEqual(campaign_run.results[0]["injections"]["masked"], 2)
        self.assertIn("Recovered 2 transient injection error(s).", campaign_run.log)

    def test_incomplete_injections_fail_the_test(self):
        failure = ExecutionResult(TerminationEvent.INTERNAL_ERROR, ExecutionState(error="startup failed"))
        injector = Mock()
        injector.run.return_value = failure
        self.injectors.append([injector])
        campaign_run = self.run_campaign([self.case])
        self.assertEqual(injector.run.call_count, 2)
        self.assertEqual(campaign_run.results, [])
        self.assertTrue(any("Injection campaign incomplete" in line for line in campaign_run.log))

    def test_injections_run_concurrently(self):
        barrier = threading.Barrier(4, timeout=5)

        def run():
            barrier.wait()
            return ExecutionResult(TerminationEvent.RETURNED, ExecutionState(stdout="ok", variables={"x": "1"}))

        injector = Mock()
        injector.run.side_effect = run
        self.injectors.append([injector])
        campaign_run = self.run_campaign([replace(self.case, execution_repetitions=4)])
        self.assertEqual(campaign_run.results[0]["injections"]["masked"], 4)


if __name__ == "__main__":
    unittest.main()
