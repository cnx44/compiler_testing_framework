import tempfile
import unittest
from pathlib import Path

from lark.exceptions import UnexpectedInput

from src.config_parser import (
    Campaign,
    ControlFlowMode,
    DataProtectionMode,
    RunnerTest,
    RunnerConfigTransformer,
    parse_runner_config,
)


class ParseRunnerConfigTest(unittest.TestCase):
    def _parse(self, source: str):
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False
        ) as config_file:
            config_file.write(source)
            config_path = config_file.name
        try:
            return parse_runner_config(config_path)
        finally:
            Path(config_path).unlink(missing_ok=True)

    def test_returns_typed_campaign_with_all_fields(self):
        campaign = self._parse(
            """name: test1
programs: [main.c, utils.cpp]
data_protection: eddi
control_flow_protection: inter-rasm
compiler_flags: [-lm, -DTRACE=1, -I../include]
functions: [main, helper]
vars: [x, y]
"""
        )

        self.assertIsInstance(campaign, Campaign)
        self.assertEqual(
            campaign.runs,
            [
                RunnerTest(
                    name="test1",
                    programs=["main.c", "utils.cpp"],
                    data_protection=DataProtectionMode.EDDI,
                    control_flow_protection=ControlFlowMode.INTER_RASM,
                    compiler_flags=["-lm", "-DTRACE=1", "-I../include"],
                    functions=["main", "helper"],
                    vars=["x", "y"],
                )
            ],
        )

    def test_parses_multiple_runs_and_optional_fields(self):
        campaign = self._parse(
            """name: first
programs: [one.c]
data_protection: none
control_flow_protection: none
name: second
programs: [two.cpp, notes.rs]
data_protection: seddi
control_flow_protection: cfcss
functions: []
vars: []
"""
        )

        self.assertEqual(len(campaign.runs), 2)
        self.assertEqual(campaign.runs[0].functions, [])
        self.assertEqual(campaign.runs[0].vars, [])
        self.assertEqual(campaign.runs[0].compiler_flags, [])
        self.assertEqual(campaign.runs[1].programs, ["two.cpp", "notes.rs"])
        self.assertIs(
            campaign.runs[0].data_protection,
            DataProtectionMode.NONE,
        )
        self.assertIs(
            campaign.runs[1].control_flow_protection,
            ControlFlowMode.CFCSS,
        )

    def test_maps_all_data_protection_modes(self):
        transformer = RunnerConfigTransformer()
        expected = {
            "eddi": DataProtectionMode.EDDI,
            "seddi": DataProtectionMode.SEDDI,
            "fdsc": DataProtectionMode.FDSC,
            "none": DataProtectionMode.NONE,
        }
        for text, mode in expected.items():
            with self.subTest(mode=text):
                self.assertIs(transformer.DATA_PROTECTION(text), mode)

    def test_maps_all_control_flow_modes(self):
        transformer = RunnerConfigTransformer()
        expected = {
            "cfcss": ControlFlowMode.CFCSS,
            "rasm": ControlFlowMode.RASM,
            "inter-rasm": ControlFlowMode.INTER_RASM,
            "none": ControlFlowMode.NONE,
        }
        for text, mode in expected.items():
            with self.subTest(mode=text):
                self.assertIs(transformer.CONTROL_FLOW(text), mode)

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(UnexpectedInput):
            self._parse(
                """name: broken
programs: [main.py]
data_protection: eddi
control_flow_protection: cfcss
"""
            )

    def test_runner_test_is_immutable(self):
        run = RunnerTest(
            "sample",
            ["main.c"],
            DataProtectionMode.EDDI,
            ControlFlowMode.CFCSS,
        )
        with self.assertRaises(AttributeError):
            run.name = "changed"


if __name__ == "__main__":
    unittest.main()
