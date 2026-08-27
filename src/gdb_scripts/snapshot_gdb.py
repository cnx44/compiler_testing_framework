import gdb
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import continue_to_function_return, run_to_function


def main():
    target_func = os.environ.get("TARGET_FUNC", "main")
    vars_to_check_str = os.environ.get("VARS_TO_CHECK", "")

    vars_to_check = [v.strip() for v in vars_to_check_str.split(",") if v.strip()]

    run_to_function(target_func)
    continue_to_function_return(target_func)

    # Extract snapshot data
    gdb.execute('printf "@@ARGS@@\\n"')
    try:
        gdb.execute("info args")
    except gdb.error:
        pass

    gdb.execute('printf "@@LOCALS@@\\n"')
    try:
        gdb.execute("info locals")
    except gdb.error:
        pass

    gdb.execute('printf "@@GLOBALS@@\\n"')
    for var_name in vars_to_check:
        gdb.execute(f'printf "{var_name} = "')
        try:
            gdb.execute(f"output {var_name}")
        except gdb.error as e:
            print(f"<error: {e}>")
        gdb.execute('printf "\\n"')

    gdb.execute('printf "@@REGISTERS@@\\n"')
    try:
        gdb.execute("info registers")
    except gdb.error:
        pass

    gdb.execute("quit")

main()
