"""Stop at the return of TARGET_FUNC and dump the program state."""
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import continue_and_dump_state, run_script, start_at_function


def main():
    start_at_function(os.environ["TARGET_FUNC"])
    continue_and_dump_state()


run_script(main)
