import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from src.campaign_runner import CampaignRun
from src.config_parser import Campaign, RunnerTest, DataProtectionMode, ControlFlowMode, TargetFunction, TargetVariable
from src.models import ExecutionResult, ExecutionState, TerminationEvent
from src.env import Environment


class CampaignRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "main.c"
        self.source.touch()
        self.case = RunnerTest("test", [str(self.source)], DataProtectionMode.NONE,
                              ControlFlowMode.NONE, functions=[TargetFunction("main")])
        self.results = {"returncode": 0, "stdout": "", "stderr": "", "variables": {},
                        "execution_time_seconds": 1.0, "binary_size_bytes": 100}

    def runner(self, runs):
        return CampaignRun(Campaign(runs), Environment(), self.root / "campaign.log")

    def mock_runner(self):
        runner = Mock()
        runner.execute.return_value = self.results
        runner.results = self.results
        return runner

    def test_cache_respects_effective_measurements_and_timeout(self):
        runs = [self.case, replace(self.case, name="reps", execution_repetitions=2),
                replace(self.case, name="timeout", timeout_ms=100),
                replace(self.case, name="reuse")]
        with (patch("src.campaign_runner.OracleRunner", side_effect=lambda *a, **k: self.mock_runner()) as oracle,
              patch("src.campaign_runner.AspisRunner", side_effect=lambda *a, **k: self.mock_runner()) as aspis,
              patch("src.campaign_runner.BaseInjector.create_for_target", return_value=[])):
            runner = self.runner(runs)
            runner.run_campaign()
        self.assertEqual(len(runner.results), 4)
        self.assertEqual(oracle.call_count, 3)
        self.assertEqual(aspis.call_count, 3)

    def test_explicit_measurements_do_not_depend_on_injection_repetitions(self):
        run = replace(self.case, execution_repetitions=2, measurement_repetitions=7)
        oracle_runner, aspis_runner = self.mock_runner(), self.mock_runner()
        with (patch("src.campaign_runner.OracleRunner", return_value=oracle_runner) as oracle,
              patch("src.campaign_runner.AspisRunner", return_value=aspis_runner),
              patch("src.campaign_runner.BaseInjector.create_for_target", return_value=[])):
            runner = self.runner([run, replace(run, name="second", execution_repetitions=3)])
            runner.run_campaign()
        oracle.assert_called_once()
        oracle_runner.execute.assert_called_once_with([], [], 7, ["main"], measure_overhead=True)
        self.assertEqual(len(runner.results), 2)

    def test_failed_snapshot_releases_runner_and_does_not_poison_cache(self):
        failed = self.mock_runner()
        failed.execute.side_effect = RuntimeError("snapshot failure")
        success = self.mock_runner()
        with (patch("src.campaign_runner.OracleRunner", side_effect=[failed, success]),
              patch("src.campaign_runner.AspisRunner", return_value=self.mock_runner()),
              patch("src.campaign_runner.BaseInjector.create_for_target", return_value=[])):
            runner = self.runner([self.case, replace(self.case, name="retry")])
            runner.run_campaign()
        failed.cleanup.assert_called_once()
        success.cleanup.assert_called_once()
        self.assertEqual(len(runner.results), 1)
        self.assertEqual(runner.results[0]["name"], "retry")

    def test_bad_arguments_file_does_not_abort_other_tests(self):
        other = self.root / "other.c"
        other.touch()
        self.source.with_suffix(".values").write_bytes(b"\xff")
        with (patch("src.campaign_runner.OracleRunner", return_value=self.mock_runner()),
              patch("src.campaign_runner.AspisRunner", return_value=self.mock_runner()),
              patch("src.campaign_runner.BaseInjector.create_for_target", return_value=[])):
            runner = self.runner([self.case, replace(self.case, name="second", programs=[str(other)])])
            runner.run_campaign()
        self.assertEqual([result["name"] for result in runner.results], ["second"])
        self.assertIn("Unable to read arguments", runner.log_path.read_text())

    def test_classification_uses_injected_functions_own_snapshot(self):
        functions = [TargetFunction(name, [TargetVariable("x")]) for name in ("first", "second")]
        self.results.update(variables={"x": "2"}, snapshots={
            "first": {"variables": {"x": "1"}}, "second": {"variables": {"x": "2"}}})
        injectors = []
        for value in ("1", "2"):
            injector = Mock()
            injector.run.return_value = ExecutionResult(TerminationEvent.RETURNED, ExecutionState(variables={"x": value}))
            injectors.append([injector])
        with (patch("src.campaign_runner.OracleRunner", return_value=self.mock_runner()),
              patch("src.campaign_runner.AspisRunner", return_value=self.mock_runner()),
              patch("src.campaign_runner.BaseInjector.create_for_target", side_effect=injectors)):
            runner = self.runner([replace(self.case, functions=functions)])
            runner.run_campaign()
        self.assertEqual(runner.results[0]["injections"]["masked"], 2)
        self.assertEqual(runner.results[0]["injections"]["latent"], 0)

    def test_debugger_failures_are_retried_not_counted_as_target_crashes(self):
        injector = Mock()
        injector.run.return_value = ExecutionResult(TerminationEvent.INTERNAL_ERROR, ExecutionState(error="startup failed"))
        with (patch("src.campaign_runner.OracleRunner", return_value=self.mock_runner()),
              patch("src.campaign_runner.AspisRunner", return_value=self.mock_runner()),
              patch("src.campaign_runner.BaseInjector.create_for_target", return_value=[injector])):
            runner = self.runner([self.case])
            runner.run_campaign()
        self.assertEqual(injector.run.call_count, 2)
        self.assertEqual(runner.results, [])
        self.assertTrue(any("Injection campaign incomplete" in line for line in runner.log))
