# gdb_utils.py
import gdb
import os

def continue_and_dump_state():
    """Continues execution to the return line and dumps the state."""
    src_file = os.environ.get("SRC_FILE")
    return_line = os.environ.get("RETURN_LINE")
    
    if src_file and return_line:
        gdb.execute(f"break {src_file}:{return_line}")
        try:
            gdb.execute("continue")
        except gdb.error:
            pass # Continue to dump state even if it crashes
            
        try:
            gdb.execute('printf "@@ARGS@@\\n"')
            gdb.execute("info args")
            
            gdb.execute('printf "@@LOCALS@@\\n"')
            gdb.execute("info locals")
            
            gdb.execute('printf "@@REGISTERS@@\\n"')
            gdb.execute("info registers")
        except gdb.error:
            pass
    else:
        try:
            gdb.execute("continue")
        except gdb.error:
            pass