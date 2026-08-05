import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Mock the gdb module which is only available inside GDB's embedded Python
gdb_mock = MagicMock()
# Provide a dummy exception class for gdb.error
class GdbError(Exception):
    pass
gdb_mock.error = GdbError
sys.modules['gdb'] = gdb_mock

# Add gdb_scripts to path so we can import gdb_utils
current_dir = os.path.dirname(os.path.abspath(__file__))
gdb_scripts_dir = os.path.join(current_dir, '..', 'src', 'gdb_scripts')
sys.path.append(gdb_scripts_dir)

import gdb_utils

class TestGdbUtils(unittest.TestCase):
    def setUp(self):
        gdb_mock.reset_mock()
        gdb_mock.execute.side_effect = None
        gdb_mock.selected_frame.side_effect = None
        gdb_mock.selected_frame.return_value = MagicMock()

    @patch('os.environ.get')
    def test_continue_and_dump_state_handles_crash(self, mock_env_get):
        # Simulate environment variables set by the injector
        env_vars = {"ENTRY_FUNC": "main"}
        mock_env_get.side_effect = lambda k, default=None: env_vars.get(k, default)
        
        # Simulate a crash during 'continue' by raising gdb.error
        def mock_execute(cmd):
            if cmd == "continue":
                raise gdb_mock.error("Program received signal SIGSEGV, Segmentation fault.")
        
        gdb_mock.execute.side_effect = mock_execute
        gdb_mock.parse_and_eval.return_value = 0x1000
        mock_block = MagicMock()
        mock_block.start = 0x1000
        mock_block.end = 0x1050
        gdb_mock.block_for_pc.return_value = mock_block
        
        mock_sym = MagicMock()
        mock_entry = MagicMock()
        mock_entry.pc = 0x1020
        mock_entry.line = 42
        mock_sym.symtab.linetable.return_value = [mock_entry]
        gdb_mock.lookup_symbol.return_value = (mock_sym, None)
        
        # This should NOT raise an exception because gdb_utils catches gdb.error
        gdb_utils.continue_and_dump_state()
        
        # Verify that it still attempted to dump the state despite the crash
        gdb_mock.execute.assert_any_call('printf "@@ARGS@@\\n"')
        gdb_mock.execute.assert_any_call('info args')
        gdb_mock.execute.assert_any_call('printf "@@LOCALS@@\\n"')
        gdb_mock.execute.assert_any_call('info locals')
        gdb_mock.execute.assert_any_call('printf "@@REGISTERS@@\\n"')
        gdb_mock.execute.assert_any_call('info registers')

    @patch('os.environ.get')
    def test_continue_and_dump_state_normal_execution(self, mock_env_get):
        env_vars = {"ENTRY_FUNC": "main"}
        mock_env_get.side_effect = lambda k, default=None: env_vars.get(k, default)
        
        gdb_mock.parse_and_eval.return_value = 0x1000
        mock_block = MagicMock()
        mock_block.start = 0x1000
        mock_block.end = 0x1050
        gdb_mock.block_for_pc.return_value = mock_block
        
        mock_sym = MagicMock()
        mock_entry = MagicMock()
        mock_entry.pc = 0x1020
        mock_entry.line = 42
        mock_sym.symtab.linetable.return_value = [mock_entry]
        gdb_mock.lookup_symbol.return_value = (mock_sym, None)
        
        # No exception is raised
        gdb_utils.continue_and_dump_state()
        
        # Verify it still dumps state
        gdb_mock.execute.assert_any_call('printf "@@ARGS@@\\n"')
        gdb_mock.execute.assert_any_call('info registers')

    def test_get_arch_config_x86_64(self):
        mock_arch = MagicMock()
        mock_arch.name.return_value = "i386:x86-64"
        mock_frame = MagicMock()
        mock_frame.architecture.return_value = mock_arch
        gdb_mock.selected_frame.return_value = mock_frame
        
        config = gdb_utils.get_arch_config()
        self.assertEqual(config["arg1_reg"], "rdi")
        self.assertEqual(config["arg3_reg"], "rdx")
        self.assertIn("rsp", config["pointer_blacklist"])

    def test_get_arch_config_aarch64(self):
        mock_arch = MagicMock()
        mock_arch.name.return_value = "aarch64"
        mock_frame = MagicMock()
        mock_frame.architecture.return_value = mock_arch
        gdb_mock.selected_frame.return_value = mock_frame
        
        config = gdb_utils.get_arch_config()
        self.assertEqual(config["arg1_reg"], "x0")
        self.assertEqual(config["arg3_reg"], "x2")
        self.assertIn("x29", config["pointer_blacklist"])

    def test_get_arch_config_no_frame(self):
        gdb_mock.selected_frame.side_effect = gdb_mock.error("No stack.")
        
        config = gdb_utils.get_arch_config()
        self.assertEqual(config["arg1_reg"], "rdi")
        self.assertEqual(config["arg3_reg"], "rdx")

if __name__ == '__main__':
    unittest.main()
