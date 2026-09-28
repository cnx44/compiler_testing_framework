"""Count the instructions TARGET_FUNC executes before its return line and list
the registers its code uses.

Prints ``PROFILED_STEPS=<n>`` and ``PROFILED_REGS=<comma-separated names>``.
"""
import os
import re
import sys

import gdb

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import function_end_line, run_script, start_at_function


def main():
    function = os.environ["TARGET_FUNC"]
    start_at_function(function)

    # Every word of the disassembly that GDB can evaluate as $word is a register.
    registers = set()
    disassembly = gdb.execute(f"disassemble {function}", to_string=True)
    for word in set(re.findall(r"\b\w+\b", disassembly)):
        try:
            if gdb.parse_and_eval(f"${word}").type.code != gdb.TYPE_CODE_VOID:
                registers.add(word)
        except gdb.error:
            pass

    # nexti steps over calls, so only the function's own instructions count.
    steps = 0
    return_line = function_end_line(function)
    try:
        while True:
            frame = gdb.newest_frame()
            if frame.name() != function or frame.find_sal().line == return_line:
                break
            gdb.execute("nexti", to_string=True)
            steps += 1
    except gdb.error:
        pass

    # One step less, so that the injection never lands on the return line.
    print(f"PROFILED_STEPS={max(0, steps - 1)}")
    print(f"PROFILED_REGS={','.join(sorted(registers))}")


run_script(main)
