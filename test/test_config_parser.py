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
    RegisterInjection,
    MemoryInjection,
    GlobalInjection,
    TargetFunction,
    TargetVariable,
)


class ParseRunnerConfigTest(unittest.TestCase):
    def _parse(self, source: str):
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False
        ) as config_file:
            config_file.write(source)
            config_path = config_file.name
        try:
            self.config_dir = Path(config_path).parent.resolve()
            return parse_runner_config(config_path)
        finally:
            Path(config_path).unlink(missing_ok=True)

    def test_returns_typed_campaign_with_all_fields(self):
        campaign = self._parse(
            """name: test1
programs: [main.c, utils.cpp]
data_protection: eddi
control_flow_protection: inter-rasm
timeout_ms: 6000
execution_repetitions: 25
compiler_flags: [-lm, -DTRACE=1, -I../include]
injection: memory
functions: [main(var1:10, var2), helper]
"""
        )

        self.assertIsInstance(campaign, Campaign)
        self.assertEqual(
            campaign.runs,
            [
                RunnerTest(
                    name="test1",
                    programs=[
                        str(self.config_dir / "main.c"),
                        str(self.config_dir / "utils.cpp"),
                    ],
                    data_protection=DataProtectionMode.EDDI,
                    control_flow_protection=ControlFlowMode.INTER_RASM,
                    timeout_ms=6000,
                    execution_repetitions=25,
                    compiler_flags=["-lm", "-DTRACE=1", "-I../include"],
                    injection=MemoryInjection(),
                    functions=[
                        TargetFunction("main", [TargetVariable("var1", 10), TargetVariable("var2", None)]),
                        TargetFunction("helper", [])
                    ],
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
injection: register data_only: true
functions: []
"""
        )

        self.assertEqual(len(campaign.runs), 2)
        self.assertEqual(campaign.runs[0].functions, [TargetFunction("main")])
        self.assertEqual(campaign.runs[0].compiler_flags, [])
        self.assertEqual(campaign.runs[0].execution_repetitions, 1)
        self.assertEqual(campaign.runs[0].timeout_ms, 5000)
        self.assertEqual(campaign.runs[0].injection, RegisterInjection())
        self.assertEqual(
            campaign.runs[1].programs,
            [
                str(self.config_dir / "two.cpp"),
                str(self.config_dir / "notes.rs"),
            ],
        )
        self.assertEqual(campaign.runs[1].injection, RegisterInjection(data_only=True))
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

    def test_parses_global_injection_target(self):
        campaign = self._parse(
            """name: global_test
programs: [main.c]
data_protection: none
control_flow_protection: none
injection: global target_global: counter
"""
        )

        self.assertEqual(campaign.runs[0].injection, GlobalInjection("counter"))

    def test_resolves_program_paths_relative_to_configuration_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "configs"
            source_dir = root / "sources"
            config_dir.mkdir()
            source_dir.mkdir()
            source = source_dir / "main.c"
            source.touch()
            config = config_dir / "campaign.cfg"
            config.write_text(
                """name: portable
programs: [../sources/main.c]
data_protection: none
control_flow_protection: none
""",
                encoding="utf-8",
            )

            campaign = parse_runner_config(config)

        self.assertEqual(campaign.runs[0].programs, [str(source.resolve())])

    def test_accepts_quoted_program_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source files" / "main.c"
            source.parent.mkdir()
            source.touch()
            config = root / "campaign.cfg"
            config.write_text(
                f'''name: quoted
programs: ["{source}"]
data_protection: none
control_flow_protection: none
''',
                encoding="utf-8",
            )

            campaign = parse_runner_config(config)

        self.assertEqual(campaign.runs[0].programs, [str(source.resolve())])

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(UnexpectedInput):
            self._parse(
                """name: broken
programs: [main.py]
data_protection: eddi
control_flow_protection: cfcss
"""
            )

    def test_rejects_non_positive_execution_repetitions(self):
        with self.assertRaises(UnexpectedInput):
            self._parse(
                """name: broken
programs: [main.c]
data_protection: eddi
control_flow_protection: cfcss
execution_repetitions: 0
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
