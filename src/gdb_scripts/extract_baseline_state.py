import gdb
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import continue_and_dump_state


try:
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    gdb.execute("start")
    gdb.execute("delete")
    continue_and_dump_state()
finally:
    gdb.execute("quit")
