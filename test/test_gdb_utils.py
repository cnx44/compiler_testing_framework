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

    @patch('os.environ.get')
    def test_continue_and_dump_state_handles_crash(self, mock_env_get):
        # Simulate environment variables set by the injector
        env_vars = {"SRC_FILE": "main.c", "RETURN_LINE": "42"}
        mock_env_get.side_effect = lambda k, default=None: env_vars.get(k, default)
        
        # Simulate a crash during 'continue' by raising gdb.error
        def mock_execute(cmd):
            if cmd == "continue":
                raise gdb_mock.error("Program received signal SIGSEGV, Segmentation fault.")
        
        gdb_mock.execute.side_effect = mock_execute
        
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
        env_vars = {"SRC_FILE": "main.c", "RETURN_LINE": "42"}
        mock_env_get.side_effect = lambda k, default=None: env_vars.get(k, default)
        
        # No exception is raised
        gdb_utils.continue_and_dump_state()
        
        # Verify it still dumps state
        gdb_mock.execute.assert_any_call('printf "@@ARGS@@\\n"')
        gdb_mock.execute.assert_any_call('info registers')

if __name__ == '__main__':
    unittest.main()
