import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.env import Environment
from src.front_end import (
    AspisTui,
    ConfigurationDraft,
    _aspis_command_preview,
    _campaign_from_draft,
    _configuration_text,
    _draft_errors,
    _draft_from_run,
    _draft_text,
    _fit_lines,
    _load_toolchain,
    main,
    _save_toolchain,
    _toolchain_errors,
    _validate_config_text,
)
from src.config_parser import (
    ControlFlowMode,
    DataProtectionMode,
    MemoryInjection,
    RunnerTest,
    TargetFunction,
    TargetVariable,
)


class FrontEndSupportTest(unittest.TestCase):
    def test_editor_output_is_accepted_by_the_campaign_parser(self):
        text = _configuration_text(
            "demo", ["main.c"], "eddi", "cfcss", "5000", "1",
            "-O2, -lm", "register data_only: true", "main(total:2)",
        )

        self.assertIn("programs: [\"main.c\"]", text)
        self.assertIsNone(_validate_config_text(text))

    def test_fit_lines_wraps_and_honours_height(self):
        self.assertEqual(_fit_lines(["abcdef", "gh"], 3, 2), ["abc", "def"])

    def test_batch_draft_creates_one_run_per_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "alpha.c", root / "beta.cpp"]
            for source in sources:
                source.touch()
            draft = ConfigurationDraft(
                name="batch",
                programs=[str(source) for source in sources],
                batch_directory=str(root),
                data_mode="seddi",
                control_mode="rasm",
                repetitions="7",
                compiler_flags="-O2, -lm",
                injection="memory",
                functions="main(result)",
            )

            campaign = _campaign_from_draft(draft)

        self.assertEqual([run.name for run in campaign.runs], ["batch-alpha", "batch-beta"])
        self.assertEqual(
            [run.programs for run in campaign.runs],
            [[str(sources[0])], [str(sources[1])]],
        )
        for run in campaign.runs:
            self.assertEqual(run.execution_repetitions, 7)
            self.assertEqual(run.compiler_flags, ["-O2", "-lm"])
            self.assertEqual(run.injection, MemoryInjection())

    def test_existing_run_round_trip_keeps_all_editable_fields(self):
        run = RunnerTest(
            name="existing",
            programs=["/different/source tree/main.c"],
            data_protection=DataProtectionMode.FDSC,
            control_flow_protection=ControlFlowMode.INTER_RASM,
            timeout_ms=8123,
            execution_repetitions=9,
            compiler_flags=["-O3", "-lm"],
            injection=MemoryInjection(),
            functions=[TargetFunction("work", [TargetVariable("items", 4)])],
        )

        edited = _campaign_from_draft(_draft_from_run(run)).runs[0]

        self.assertEqual(edited, run)

    def test_draft_validation_reports_errors_without_mutating_values(self):
        draft = ConfigurationDraft(
            name="invalid name",
            programs=["/missing/main.c"],
            timeout="0",
            repetitions="abc",
        )

        errors = _draft_errors(draft)

        self.assertEqual(draft.name, "invalid name")
        self.assertTrue(any("Name" in error for error in errors))
        self.assertTrue(any("Source file not found" in error for error in errors))
        self.assertTrue(any("Timeout" in error for error in errors))
        self.assertTrue(any("Execution repetitions" in error for error in errors))

    def test_draft_serialization_quotes_sources_outside_config_directory(self):
        draft = ConfigurationDraft(programs=["/tmp/source files/main.c"])

        text = _draft_text(draft)

        self.assertIn('programs: ["/tmp/source files/main.c"]', text)

    def test_compilation_error_opens_log_and_can_return_to_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "campaign.cfg"
            config.touch()
            tui = AspisTui(Environment(), start_dir=Path(directory))
            tui.screen = MagicMock()
            failed_runner = MagicMock()
            failed_runner.results = []
            failed_runner.log = ["RUN NAME: broken", "ERROR: compiler failed"]
            with (
                patch.object(tui, "_file_picker", return_value=config),
                patch.object(tui, "_frame"),
                patch.object(tui, "_write"),
                patch.object(tui, "_paged_text") as paged_text,
                patch(
                    "src.front_end.parse_runner_config",
                    return_value=MagicMock(),
                ),
                patch("src.front_end.CampaignRun", return_value=failed_runner),
            ):
                tui._run_test()

        paged_text.assert_called_once_with(
            "ASPIS · Campaign failed",
            failed_runner.log,
            "↑↓ scroll · Esc return to file selection",
        )

    def test_toolchain_configuration_round_trip_preserves_aspis_flags(self):
        environment = Environment(
            aspis_path="/opt/aspis/aspis.sh",
            vanilla_compiler="clang-21",
            llvm_path="/opt/llvm/bin",
            gdb_executable="gdb",
            lldb_executable="gdb",
            aspis_flags=["--no-cleanup", "--coarse-grained"],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".aspis-tui.json"
            _save_toolchain(path, environment)
            loaded = _load_toolchain(path, Environment())

        self.assertEqual(loaded, environment)

    def test_toolchain_validation_checks_required_programs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "aspis.sh"
            script.touch(mode=0o755)
            llvm = root / "llvm"
            llvm.mkdir()
            for executable in ("clang", "opt", "llvm-link"):
                (llvm / executable).touch()
            environment = Environment(
                aspis_path=script,
                llvm_path=llvm,
                vanilla_compiler="clang",
                gdb_executable="gdb",
            )
            with patch("src.front_end.shutil.which", return_value="/usr/bin/tool"):
                errors = _toolchain_errors(environment)

        self.assertEqual(errors, [])

    def test_aspis_command_preview_keeps_flags_separate(self):
        environment = Environment(
            aspis_path="/opt/aspis.sh",
            llvm_path="/opt/llvm/bin",
            aspis_flags=["--no-cleanup"],
        )

        preview = _aspis_command_preview(environment)

        self.assertIn("/opt/aspis.sh --llvm-bin /opt/llvm/bin", preview)
        self.assertIn("--no-cleanup", preview)
        self.assertIn("<source.c>", preview)

    def test_main_loads_project_toolchain_configuration(self):
        saved = Environment(
            aspis_path="/opt/aspis.sh",
            vanilla_compiler="clang-21",
            llvm_path="/opt/llvm/bin",
            gdb_executable="gdb",
            lldb_executable="gdb",
            aspis_flags=["--no-cleanup"],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _save_toolchain(root / ".aspis-tui.json", saved)
            with (
                patch("src.front_end.Path.cwd", return_value=root),
                patch("src.front_end.AspisTui") as tui,
                patch("sys.argv", ["front_end"]),
                patch.dict("os.environ", {}, clear=True),
            ):
                main()

        self.assertEqual(tui.call_args.args[0], saved)
        self.assertEqual(tui.call_args.kwargs["start_dir"], root)
        tui.return_value.run.assert_called_once_with()
