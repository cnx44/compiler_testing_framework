import unittest
import tempfile
from pathlib import Path

from src.config_builder import campaign_to_text
from src.config_parser import (
    Campaign,
    ControlFlowMode,
    DataProtectionMode,
    GlobalInjection,
    RunnerTest,
    TargetFunction,
    TargetVariable,
    parse_runner_config,
)


class ConfigBuilderTest(unittest.TestCase):
    def test_serializes_a_campaign_in_the_parser_grammar(self):
        main_source = str(Path("/sources/main.c"))
        utils_source = str(Path("/sources/lib files/utils.cpp"))
        campaign = Campaign([
            RunnerTest(
                name="demo",
                programs=[main_source, utils_source],
                data_protection=DataProtectionMode.SEDDI,
                control_flow_protection=ControlFlowMode.INTER_RASM,
                timeout_ms=7000,
                execution_repetitions=3,
                compiler_flags=["-O2", "-lm"],
                injection=GlobalInjection("counter"),
                functions=[TargetFunction("work", [TargetVariable("sum", 4)])],
            )
        ])

        text = campaign_to_text(campaign)
        self.assertEqual(
            text,
            (
                "name: demo\n"
                f'programs: ["{main_source}", "{utils_source}"]\n'
                "data_protection: seddi\n"
                "control_flow_protection: inter-rasm\n"
                "timeout_ms: 7000\n"
                "execution_repetitions: 3\n"
                "compiler_flags: [-O2, -lm]\n"
                "injection: global target_global: counter\n"
                "functions: [work(sum:4)]\n"
            ),
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as file:
            file.write(text)
            path = Path(file.name)
        try:
            self.assertEqual(parse_runner_config(path), campaign)
        finally:
            path.unlink(missing_ok=True)
