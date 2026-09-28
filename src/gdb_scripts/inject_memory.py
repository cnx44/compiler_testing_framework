"""Flip one bit in memory at a random instruction of TARGET_FUNC.

With PAYLOAD_PTR the bit lands inside that variable (or the memory it points
to, LENGTH_VAR elements long); otherwise inside the function's stack frame.
"""
import os
import random
import sys

import gdb

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import (continue_and_dump_state, flip_random_bit, get_arch_config,
                       run_script, start_at_function, step_randomly)


def register_value(register):
    return int(gdb.parse_and_eval(f"(unsigned long long)${register}"))


def payload_bounds(name, length_var):
    """Return ``(address, size in bytes)`` of the payload variable."""
    value = None
    element_size = 4
    try:
        value = gdb.parse_and_eval(name)
        value_type = value.type.strip_typedefs()
        if value_type.code in (gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_ARRAY):
            element_size = int(value_type.target().sizeof)
        else:
            element_size = int(value_type.sizeof)
        if value_type.code == gdb.TYPE_CODE_PTR:
            address = int(value)
        elif value.address is None:
            raise ValueError(f"'{name}' has no memory address (likely in a register).")
        else:
            address = int(value.address)
        gdb.selected_inferior().read_memory(address, 1)
    except Exception as error:
        # Protected binaries can carry stale debug info: use the first argument.
        register = get_arch_config()["arg1_reg"]
        print(f"[DIAG] Cannot use debug info of {name} ({error}); using ${register}.")
        address = register_value(register)

    if length_var:
        try:
            elements = int(gdb.parse_and_eval(length_var))
            if not 0 <= elements <= 1_000_000:
                raise ValueError("implausible length")
        except Exception as error:
            register = get_arch_config()["arg3_reg"]
            print(f"[DIAG] Cannot evaluate {length_var} ({error}); using ${register}.")
            elements = register_value(register)
    elif value is not None and value.type.strip_typedefs().code == gdb.TYPE_CODE_ARRAY:
        elements = int(value.type.sizeof) // element_size
        print(f"[DIAG] Inferred static array length: {elements} elements")
    else:
        print("[DIAG] No length and not a static array. Defaulting to 1 element.")
        elements = 1

    size = elements * element_size
    print(f"[DIAG] Total payload size: {size} bytes")
    return address, size


def main():
    payload = os.environ.get("PAYLOAD_PTR", "")
    start_at_function(os.environ["TARGET_FUNC"])

    # The payload is located at function entry, where its arguments are intact.
    target = None
    if payload:
        address, size = payload_bounds(payload, os.environ.get("LENGTH_VAR", ""))
        if address > 0x1000 and size > 0:
            target = (address, size, f"payload {payload}")
        else:
            print("[DIAG] Invalid payload address or size; using the stack frame.")

    step_randomly(int(os.environ["MAX_STEPS"]))

    if target is None:
        sp, fp = register_value("sp"), register_value("fp")
        size = fp - sp if fp > sp + 8 else 4
        target = (sp, size, "stack frame")

    address, size, strategy = target
    byte = address + random.randrange(size)
    bit = flip_random_bit(byte, 1)
    print(f"[*] Injected memory at {byte:#x}, flipped bit {bit} ({strategy})")

    continue_and_dump_state()


run_script(main)
