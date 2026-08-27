import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.env import Environment
from src.front_end import (
    _aspis_command_preview,
    _configuration_text,
    _fit_lines,
    _load_toolchain,
    main,
    _save_toolchain,
    _toolchain_errors,
    _validate_config_text,
)


class FrontEndSupportTest(unittest.TestCase):
    def test_editor_output_is_accepted_by_the_campaign_parser(self):
        text = _configuration_text(
            "demo", ["main.c"], "eddi", "cfcss", "5000", "1",
            "-O2, -lm", "register data_only: true", "main(total:2)",
        )

        self.assertIn("programs: [main.c]", text)
        self.assertIsNone(_validate_config_text(text))

    def test_fit_lines_wraps_and_honours_height(self):
        self.assertEqual(_fit_lines(["abcdef", "gh"], 3, 2), ["abc", "def"])

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
        self.assertIn("<sorgente.c>", preview)

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
