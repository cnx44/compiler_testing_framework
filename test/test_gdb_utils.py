import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

# The gdb module only exists inside GDB's embedded Python.
gdb_mock = MagicMock()


class GdbError(Exception):
    pass


gdb_mock.error = GdbError
sys.modules["gdb"] = gdb_mock
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src", "gdb_scripts"))

import gdb_utils  # noqa: E402


class GdbUtilsTest(unittest.TestCase):
    def setUp(self):
        gdb_mock.reset_mock()
        gdb_mock.execute.side_effect = None
        gdb_mock.selected_frame.side_effect = None
        gdb_mock.selected_frame.return_value.find_sal.return_value.line = 10

    def dump(self, execute=None, env=None):
        gdb_mock.execute.side_effect = execute
        output = io.StringIO()
        with (patch.dict(os.environ, {"TARGET_FUNC": "main", **(env or {})}),
              patch.object(gdb_utils, "function_end_line", return_value=42),
              redirect_stdout(output)):
            gdb_utils.continue_and_dump_state()
        return output.getvalue()

    def test_dump_prints_sections_and_requested_variables(self):
        output = self.dump(env={"DUMP_VARS": "buf:n,G"})
        commands = [c.args[0] for c in gdb_mock.execute.call_args_list]
        self.assertEqual(commands, ["continue", "call (int)fflush(0)", "info args", "info locals",
                                    "output *buf@n", "output G", "info registers"])
        self.assertEqual(output, "@@ARGS@@\n@@LOCALS@@\nbuf = \nG = \n@@REGISTERS@@\n")
        gdb_mock.Breakpoint.assert_called_once_with("42")

    def test_crash_while_continuing_is_reported_and_state_still_dumped(self):
        def execute(command):
            if command == "continue":
                raise GdbError("SIGSEGV")
            if command == "info locals":
                raise GdbError("No frame selected.")

        output = self.dump(execute)
        self.assertIn("Program received signal: SIGSEGV", output)
        self.assertIn("@@LOCALS@@\n<error: No frame selected.>\n@@REGISTERS@@", output)

    def test_flip_random_bit_changes_exactly_one_bit(self):
        inferior = gdb_mock.selected_inferior.return_value
        inferior.read_memory.return_value = b"\x00\x00"
        bit = gdb_utils.flip_random_bit(0x1000, 2)
        written = int.from_bytes(inferior.write_memory.call_args.args[1], "little")
        self.assertEqual(written, 1 << bit)

    def test_arch_config(self):
        for name, arg1, pointer in (("i386:x86-64", "rdi", "rsp"), ("aarch64", "x0", "x29")):
            with self.subTest(arch=name):
                gdb_mock.selected_frame.return_value.architecture.return_value.name.return_value = name
                config = gdb_utils.get_arch_config()
                self.assertEqual(config["arg1_reg"], arg1)
                self.assertIn(pointer, config["pointer_blacklist"])

    def test_arch_config_without_frame_defaults_to_x86_64(self):
        gdb_mock.selected_frame.side_effect = GdbError("No stack.")
        self.assertEqual(gdb_utils.get_arch_config()["arg3_reg"], "rdx")


if __name__ == "__main__":
    unittest.main()
