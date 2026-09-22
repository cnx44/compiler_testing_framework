import curses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config_parser import GlobalInjection, parse_runner_config
from src.env import Environment
from src.front_end import AspisTui, ConfigurationDraft, _QuitTui, _validate_results


class TuiRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "main.c"
        self.source.write_text("int G; int main(void) { return G; }\n")
        self.config = self.root / "campaign.cfg"
        self.tui = AspisTui(Environment(), self.root)
        self.tui.screen = MagicMock()
        self.tui.screen.getmaxyx.return_value = (24, 80)

    def configuration(self, name="one", injection="global"):
        return (f'name: {name}\nprograms: ["{self.source}"]\n'
                'data_protection: none\ncontrol_flow_protection: none\n'
                'measure_overhead: false\nmeasurement_repetitions: 7\n'
                f'injection: {injection}\nfunctions: [main(@G)]\n')

    def test_edit_preserves_global_targets_in_all_tests(self):
        self.config.write_text(self.configuration() + self.configuration("two", "memory"))
        with (patch.object(self.tui, "_file_picker", return_value=self.config),
              patch.object(self.tui, "_choose", return_value="1. one"),
              patch.object(self.tui, "_configuration_form", side_effect=lambda draft, *a, **k: draft),
              patch.object(self.tui, "_message") as message):
            self.tui._edit_configuration()
        self.assertEqual(message.call_args.args[0], "Configuration updated")
        runs = parse_runner_config(self.config).runs
        self.assertIsInstance(runs[0].injection, GlobalInjection)
        self.assertTrue(all(run.functions[0].variables[0].is_global for run in runs))
        self.assertTrue(all(not run.measure_overhead and run.measurement_repetitions == 7 for run in runs))

    def test_delete_last_test_preserves_file(self):
        original = self.configuration()
        self.config.write_text(original)
        with (patch.object(self.tui, "_file_picker", return_value=self.config),
              patch.object(self.tui, "_configuration_form", return_value="DELETE"),
              patch.object(self.tui, "_message") as message):
            self.tui._edit_configuration()
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(message.call_args.args[0], "Cannot delete last test")

    def test_delete_requires_confirmation(self):
        original = self.configuration() + self.configuration("two")
        for choice in (None, "Cancel", "Delete"):
            with self.subTest(choice=choice):
                self.config.write_text(original)
                with (patch.object(self.tui, "_file_picker", return_value=self.config),
                      patch.object(self.tui, "_choose", side_effect=["1. one", choice]),
                      patch.object(self.tui, "_configuration_form", return_value="DELETE"),
                      patch.object(self.tui, "_message")):
                    self.tui._edit_configuration()
                names = [run.name for run in parse_runner_config(self.config).runs]
                self.assertEqual(names, ["two"] if choice == "Delete" else ["one", "two"])

    def test_exit_from_last_test_message_is_not_caught_as_save_error(self):
        self.config.write_text(self.configuration())
        with (patch.object(self.tui, "_file_picker", return_value=self.config),
              patch.object(self.tui, "_configuration_form", return_value="DELETE"),
              patch.object(self.tui, "_message", side_effect=_QuitTui) as message):
            with self.assertRaises(_QuitTui):
                self.tui._edit_configuration()
        message.assert_called_once()

    def test_overwrite_requires_confirmation(self):
        for choice in (None, "Cancel", "Replace"):
            with self.subTest(choice=choice):
                self.config.write_text("original contents")
                draft = ConfigurationDraft(programs=[str(self.source)])
                with (patch.object(self.tui, "_edit_draft_field", side_effect=lambda d, key: d.programs.append(str(self.source))),
                      patch.object(self.tui, "_configuration_form", return_value=draft),
                      patch.object(self.tui, "_file_picker", return_value=self.root),
                      patch.object(self.tui, "_ask_text", return_value="campaign.cfg"),
                      patch.object(self.tui, "_choose", return_value=choice),
                      patch.object(self.tui, "_message")):
                    self.tui._create_configuration()
                if choice == "Replace":
                    self.assertEqual(parse_runner_config(self.config).runs[0].name, "test")
                else:
                    self.assertEqual(self.config.read_text(), "original contents")

    def test_invalid_result_shapes_are_recoverable(self):
        invalid = [{"hello": "world"}, [None], [{}],
                   [{"name": "x", "injections": None}],
                   [{"name": "x", "injections": {"total": "1"}}],
                   [{"name": "x", "injections": {"sdc_states": [None]}}],
                   [{"name": "x", "oracle": {"variables": []}}],
                   [{"name": "x", "difference": {"binary_size_bytes": {"percent": "bad"}}}]]
        path = self.root / "results.json"
        for value in invalid:
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                with (patch.object(self.tui, "_file_picker", return_value=path),
                      patch.object(self.tui, "_message") as message,
                      patch.object(self.tui, "_campaign_summary") as summary):
                    self.tui._view_campaign_results()
                message.assert_called_once()
                summary.assert_not_called()
        _validate_results([])
        _validate_results([{"name": "x"}])

    def text_input(self, keys, default="-lm", allow_empty=True):
        self.tui.screen.get_wch.side_effect = keys
        with (patch.object(self.tui, "_frame"), patch.object(self.tui, "_write"),
              patch("src.front_end.curses.curs_set"), patch("src.front_end.curses.noecho")):
            return self.tui._ask_text("Flags", default, allow_empty)

    def test_text_cancel_exit_and_editing(self):
        self.assertIsNone(self.text_input(["\x1b"]))
        self.assertEqual(self.text_input(["\n"]), "-lm")
        self.assertEqual(self.text_input(["\x15", "\n"]), "")
        self.assertEqual(self.text_input(["\x15", "é", "x", curses.KEY_LEFT, curses.KEY_DC, "\n"]), "é")
        with self.assertRaises(_QuitTui):
            self.text_input([":", "q"])

    def test_form_keeps_selected_actions_above_footer(self):
        for height in (18, 24, 30):
            for selection, label in ((11, "Validate and save"), (12, "Delete test")):
                with self.subTest(height=height, selection=selection):
                    self.tui.screen.getmaxyx.return_value = (height, 60)
                    frames = []
                    with (patch.object(self.tui, "_frame", side_effect=lambda *a: frames.append([])),
                          patch.object(self.tui, "_write", side_effect=lambda r, c, text, *a: frames[-1].append((r, text))),
                          patch.object(self.tui, "_selected_attr", return_value=0),
                          patch("src.front_end.curses.color_pair", return_value=0),
                          patch.object(self.tui, "_key", side_effect=[ord("j")] * selection + [27])):
                        self.tui._configuration_form(ConfigurationDraft(), "Edit", allow_delete=True)
                    self.assertTrue(any(text == f"› {label}" for _, text in frames[-1]))
                    self.assertTrue(all(2 <= row < height - 3 for frame in frames for row, _ in frame))

    def test_long_log_summary_and_inspector_scroll_to_the_end(self):
        self.tui.screen.getmaxyx.return_value = (18, 60)
        result = {"name": "sample", "injections": {"latent_states": [{"variables": {f"v{i:02}": i for i in range(30)}}]}}
        cases = [
            (lambda: self.tui._paged_text("Log", ["x" * 2000, "THE_END"], "Scroll"), "THE_END"),
            (lambda: self.tui._campaign_summary("campaign", [result]), "ASPIS CF"),
            (lambda: self.tui._sdc_inspector("campaign", result, "latent_states", "LATENT"), "v29"),
        ]
        for action, expected in cases:
            with self.subTest(expected=expected):
                frames = []
                with (patch.object(self.tui, "_frame", side_effect=lambda *a: frames.append([])),
                      patch.object(self.tui, "_write", side_effect=lambda r, c, text, *a: frames[-1].append((r, text))),
                      patch("src.front_end.curses.color_pair", return_value=0),
                      patch.object(self.tui, "_key", side_effect=[ord("j")] * 80 + [27])):
                    action()
                self.assertTrue(any(expected in text for _, text in frames[-1]))
                self.assertTrue(all(3 <= row < 15 for frame in frames for row, _ in frame))

    def test_home_and_toolchain_actions_visible_in_small_terminal(self):
        for height in (9, 18):
            self.tui.screen.getmaxyx.return_value = (height, 60)
            for action, keys, label in ((self.tui._home, [ord("k"), 10], "› Exit"),
                                        (self.tui._configure_toolchain, [ord("k"), 27], "› Save settings")):
                with self.subTest(height=height, label=label):
                    frames = []
                    with (patch.object(self.tui, "_frame", side_effect=lambda *a: frames.append([])),
                          patch.object(self.tui, "_write", side_effect=lambda r, c, text, *a: frames[-1].append((r, text))),
                          patch.object(self.tui, "_selected_attr", return_value=0),
                          patch("src.front_end.curses.color_pair", return_value=0),
                          patch.object(self.tui, "_key", side_effect=keys)):
                        action()
                    self.assertTrue(any(label in text for _, text in frames[-1]))
                    self.assertTrue(all(2 <= row < height - 3 for frame in frames for row, _ in frame))
