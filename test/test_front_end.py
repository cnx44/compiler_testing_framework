import curses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.env import Environment
from src.front_end import (
    AspisTui,
    ConfigurationDraft,
    FileSelection,
    _QuitTui,
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
    def test_source_picker_and_injection_use_checkbox_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.c").touch()
            tui = AspisTui(Environment(), start_dir=root)
            tui.screen = MagicMock()
            tui.screen.getmaxyx.return_value = (30, 120)
            displayed = []
            with (
                patch.object(tui, "_frame"),
                patch.object(tui, "_write", side_effect=lambda row, col, label, *args: displayed.append(label)),
                patch.object(tui, "_selected_attr", return_value=0),
                patch("src.front_end.curses.color_pair", return_value=0),
                patch.object(tui, "_key", side_effect=[27]),
            ):
                tui._file_picker("Sources", multi=True, suffixes={".c"})
            self.assertTrue(any("[ ] main.c" in label for label in displayed))

            displayed.clear()
            with (
                patch.object(tui, "_frame"),
                patch.object(tui, "_write", side_effect=lambda row, col, label, *args: displayed.append(label)),
                patch.object(tui, "_selected_attr", return_value=0),
                patch.object(tui, "_key", side_effect=[27]),
            ):
                tui._edit_injection(["register data_only: false"])
            self.assertTrue(any("[✓] Register · all registers" in label for label in displayed))
            self.assertTrue(any("[ ] Register · data registers only" in label for label in displayed))

    def test_injection_enter_and_space_toggle_off_and_on(self):
        tui = AspisTui(Environment())
        keys = [10, ord(" "), 10, ord("j"), ord("j"), 10,
                ord(" "), ord(" "), ord("j"), ord("j"), 10]
        with (patch.object(tui, "_frame"), patch.object(tui, "_write"),
              patch.object(tui, "_height", return_value=30),
              patch.object(tui, "_selected_attr", return_value=0),
              patch.object(tui, "_key", side_effect=keys)):
            result = tui._edit_injection(["register data_only: false"])
        self.assertEqual(result, ["memory"])

    def test_scope_menu_edits_only_selected_file(self):
        draft = ConfigurationDraft(programs=["a.c", "b.c"], batch_directory=".")
        tui = AspisTui(Environment())
        choices = iter(["2. b.c · main", "Enter manually", "Done"])
        with patch.object(tui, "_choose", side_effect=lambda *args: next(choices)), \
             patch.object(tui, "_ask_text", return_value="work(x, @G)") as ask:
            tui._scope_menu(draft)
        self.assertEqual(draft.functions, {"a.c": "main", "b.c": "work(x, @G)"})
        ask.assert_called_once()

    def test_scope_menu_cancel_keeps_existing_scope_and_defaults_new_file(self):
        draft = ConfigurationDraft(programs=["a.c", "b.c"], functions={"a.c": "work(x)"})
        tui = AspisTui(Environment())
        with patch.object(tui, "_choose", return_value=None):
            tui._scope_menu(draft)
        self.assertEqual(draft.functions, {"a.c": "work(x)", "b.c": "main"})

    def test_invalid_config_is_written_to_run_log(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "broken.cfg"
            config.write_text("invalid configuration")
            tui = AspisTui(Environment())
            with patch.object(tui, "_message") as message:
                tui._run_configuration(config)
            self.assertIn("ERROR: CONFIGURATION", config.with_suffix(".log").read_text())
            message.assert_called_once()

    def test_source_scope_selection_preserves_global_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.c"
            source.write_text("int G; int main() { int x = 1; return x; }")
            tui = AspisTui(Environment())
            with (
                patch.object(tui, "_choose_many", side_effect=[["main · line 1"], ["x", "@G"]]),
                patch.object(tui, "_ask_text", side_effect=lambda title, default: default),
            ):
                self.assertEqual(tui._source_scope(source, "main"), "main(x, @G)")

    def test_running_screen_displays_progress_before_worker_finishes(self):
        from threading import Event

        rendered = Event()
        tui = AspisTui(Environment())
        tui.screen = MagicMock()
        tui.screen.getmaxyx.return_value = (30, 120)
        runner = MagicMock()

        def run_campaign(*, progress):
            progress({"test": "example", "index": 2, "total": 4,
                      "phase": "Compiling ASPIS", "sources": "main.c"})
            if not rendered.wait(3):
                raise AssertionError("UI did not update while compilation was running")

        def write(row, col, line):
            if "Compiling ASPIS" in line:
                rendered.set()

        runner.run_campaign.side_effect = run_campaign
        with patch.object(tui, "_frame"), patch.object(tui, "_write", side_effect=write):
            tui._run_with_progress(runner, Path("campaign.cfg"))
        self.assertTrue(rendered.is_set())

    def test_colon_q_exits_from_any_tui_screen(self):
        tui = AspisTui(Environment())
        tui.screen = MagicMock()
        tui.screen.getch.side_effect = [ord(":"), ord("q")]

        with self.assertRaises(_QuitTui):
            tui._key()

    def test_main_handles_colon_q_exit_signal(self):
        tui = AspisTui(Environment())
        screen = MagicMock()
        with (
            patch("src.front_end.curses.curs_set"),
            patch.object(tui, "_init_colors"),
            patch.object(tui, "_home", side_effect=_QuitTui),
        ):
            tui._main(screen)

    def test_home_accepts_j_for_downward_navigation(self):
        tui = AspisTui(Environment())
        tui.screen = MagicMock()
        tui.screen.getmaxyx.return_value = (24, 80)
        with (
            patch.object(tui, "_frame"),
            patch.object(tui, "_write"),
            patch.object(
                tui,
                "_key",
                side_effect=[ord("j"), curses.KEY_ENTER, _QuitTui],
            ),
            patch("src.front_end.curses.color_pair", return_value=0),
            patch.object(tui, "_create_configuration") as create,
        ):
            with self.assertRaises(_QuitTui):
                tui._home()

        create.assert_called_once_with()

    def test_plain_q_does_not_exit_from_home(self):
        tui = AspisTui(Environment())
        tui.screen = MagicMock()
        tui.screen.getmaxyx.return_value = (24, 80)
        with (
            patch.object(tui, "_frame") as frame,
            patch.object(tui, "_write"),
            patch.object(tui, "_key", side_effect=[ord("q"), _QuitTui]),
            patch("src.front_end.curses.color_pair", return_value=0),
        ):
            with self.assertRaises(_QuitTui):
                tui._home()

        self.assertEqual(frame.call_count, 2)

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

    def test_draft_creates_cartesian_product_of_aspis_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.c"
            source.touch()
            draft = ConfigurationDraft(
                name="matrix",
                programs=[str(source)],
                data_mode=["eddi", "seddi"],
                control_mode=["cfcss", "none"],
            )

            runs = _campaign_from_draft(draft).runs

        self.assertEqual(len(runs), 4)
        self.assertEqual(
            [run.name for run in runs],
            [
                "matrix-eddi-cfcss-register",
                "matrix-eddi-none-register",
                "matrix-seddi-cfcss-register",
                "matrix-seddi-none-register",
            ],
        )

    def test_file_picker_s_selects_all_visible_source_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "a.c", root / "b.cpp", root / "c.rs"]
            for source in sources:
                source.touch()
            (root / "ignored.txt").touch()
            tui = AspisTui(Environment(), start_dir=root)
            tui.screen = MagicMock()
            with (
                patch.object(tui, "_frame"),
                patch.object(tui, "_write"),
                patch.object(tui, "_height", return_value=30),
                patch.object(tui, "_key", return_value=ord("s")),
                patch("src.front_end.curses.color_pair", return_value=0),
            ):
                selected = tui._file_picker(
                    "sources", multi=True, suffixes={".c", ".cpp", ".rs"}
                )

        self.assertEqual(selected, sources)
        self.assertEqual(selected.batch_directory, root)

    def test_select_all_sources_creates_independent_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "first.c", root / "second.c"]
            for source in sources:
                source.touch()
            selection = FileSelection(sources, root)
            tui = AspisTui(Environment(), start_dir=root)
            draft = ConfigurationDraft(name="all")
            with (
                patch.object(tui, "_choose", return_value="Select files"),
                patch.object(tui, "_file_picker", return_value=selection),
            ):
                tui._edit_draft_field(draft, "programs")

            runs = _campaign_from_draft(draft).runs

        self.assertEqual([run.name for run in runs], ["all-first", "all-second"])
        self.assertEqual([len(run.programs) for run in runs], [1, 1])

    def test_selecting_multiple_files_creates_independent_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "first.c", root / "second.c"]
            for source in sources:
                source.touch()
            tui = AspisTui(Environment(), start_dir=root)
            draft = ConfigurationDraft(name="selected")
            with (
                patch.object(tui, "_choose", return_value="Select files"),
                patch.object(tui, "_file_picker", return_value=sources),
            ):
                tui._edit_draft_field(draft, "programs")

            runs = _campaign_from_draft(draft).runs

        self.assertEqual([run.name for run in runs], ["selected-first", "selected-second"])
        self.assertEqual([len(run.programs) for run in runs], [1, 1])

    def test_link_files_together_keeps_one_program(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "main.c", root / "helper.c"]
            for source in sources:
                source.touch()
            tui = AspisTui(Environment(), start_dir=root)
            draft = ConfigurationDraft(name="linked")
            with (
                patch.object(tui, "_choose", return_value="Link files together"),
                patch.object(tui, "_file_picker", return_value=sources),
            ):
                tui._edit_draft_field(draft, "programs")

            runs = _campaign_from_draft(draft).runs

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].programs, [str(path) for path in sources])

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
            tui.screen.getmaxyx.return_value = (30, 120)
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
            "↑↓/jk scroll · Esc return to file selection",
        )

    def test_toolchain_configuration_round_trip_preserves_aspis_flags(self):
        environment = Environment(
            aspis_path="/opt/aspis/aspis.sh",
            vanilla_compiler="clang-21",
            llvm_path="/opt/llvm/bin",
            gdb_executable="gdb",
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
