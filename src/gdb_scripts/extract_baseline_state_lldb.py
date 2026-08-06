import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from lldb_utils import continue_and_dump_state, run_to_function


def __lldb_init_module(debugger, internal_dict):
    try:
        function = os.environ.get("TARGET_FUNC", "main")
        target, process, _ = run_to_function(debugger, function)
        continue_and_dump_state(debugger, target, process)
    finally:
        debugger.HandleCommand("quit")

