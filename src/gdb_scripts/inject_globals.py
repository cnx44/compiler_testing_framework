"""Flip one bit of a global variable (TARGET_GLOBAL, or a random global of
TARGET_SRC) at a random instruction of TARGET_FUNC."""
import os
import random
import re
import sys

import gdb

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import (continue_and_dump_state, flip_random_bit, run_script,
                       start_at_function, step_randomly)


def source_globals(source):
    """Names of the variables that ``info variables`` lists for ``source``."""
    names = []
    in_debug_symbols = False
    in_source = not source
    for line in gdb.execute("info variables", to_string=True).splitlines():
        line = line.strip()
        if line.startswith("All defined variables:"):
            in_debug_symbols = True
        elif line.startswith("Non-debugging symbols:"):
            in_debug_symbols = False
        elif in_debug_symbols and line.startswith("File "):
            in_source = not source or source in line
        elif in_debug_symbols and in_source:
            # e.g. "12:	int counter;" or "static float table[10];"
            match = re.search(r"\b([A-Za-z_]\w*)(?:\s*\[.*\])?\s*;", line)
            if match:
                names.append(match.group(1))
    return names


def main():
    name = os.environ.get("TARGET_GLOBAL", "").strip()
    if not name:
        # Listed before starting the program: ``info variables`` is slow later.
        candidates = source_globals(os.environ.get("TARGET_SRC", ""))
        if not candidates:
            raise RuntimeError("No global variables found to inject.")
        name = random.choice(candidates)

    start_at_function(os.environ["TARGET_FUNC"])
    step_randomly(int(os.environ["MAX_STEPS"]))

    value = gdb.parse_and_eval(name)
    address = int(value.address)
    # Large variables (arrays) are only injected in their first 8 bytes.
    size = min(int(value.type.sizeof), 8)
    bit = flip_random_bit(address, size)
    print(f"[*] Injected global {name} at {address:#x}, flipped bit {bit}")

    continue_and_dump_state()


run_script(main)
