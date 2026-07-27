import tempfile
import unittest
from pathlib import Path

from src.config_parser import (
    Campaign,
    ControlFlowMode,
    DataProtectionMode,
    RunnerTest,
    parse_runner_config,
)


class ParseRunnerConfigTest(unittest.TestCase):
    def test_parse_runner_config_returns_typed_campaign(self):
        config = """name: test1
        programs: [main.c, utils.cpp]
        data_protection: eddi
        control_flow_protection: inter-rasm
        functions: [main, helper]
        vars: [x, y]
        """

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as file:
            file.write(config)
            config_path = file.name

        try:
            campaign = parse_runner_config(config_path)
        finally:
            Path(config_path).unlink(missing_ok=True)

        self.assertIsInstance(campaign, Campaign)
        self.assertEqual(len(campaign.runs), 1)

        run = campaign.runs[0]
        self.assertIsInstance(run, RunnerTest)
        self.assertEqual(run.name, "test1")
        self.assertEqual(run.programs, ["main.c", "utils.cpp"])
        self.assertIs(run.data_protection, DataProtectionMode.EDDI)
        self.assertIs(run.control_flow_protection, ControlFlowMode.INTER_RASM)
        self.assertEqual(run.functions, ["main", "helper"])
        self.assertEqual(run.vars, ["x", "y"])


if __name__ == "__main__":
    unittest.main()
