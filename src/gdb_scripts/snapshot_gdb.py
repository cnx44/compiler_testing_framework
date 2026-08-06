import gdb
import os
import sys

def main():
    target_src = os.environ.get("TARGET_SRC")
    return_line = os.environ.get("RETURN_LINE")
    vars_to_check_str = os.environ.get("VARS_TO_CHECK", "")
    
    if not target_src or not return_line:
        print("Error: TARGET_SRC and RETURN_LINE environment variables are required.")
        sys.exit(1)

    vars_to_check = [v.strip() for v in vars_to_check_str.split(",") if v.strip()]

    # Set breakpoint and run
    gdb.execute(f"break {target_src}:{return_line}")
    gdb.execute("run")

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
