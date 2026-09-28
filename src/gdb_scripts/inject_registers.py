"""Flip one bit of a register used by TARGET_FUNC at a random instruction."""
import os
import random
import sys

import gdb

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import (continue_and_dump_state, get_arch_config, run_script,
                       start_at_function, step_randomly)

VECTOR_BYTE_FIELDS = ("v16_int8", "v8_int8", "v32_int8", "v64_int8", "b")


def flip_register_bit(register, bit):
    try:
        value = int(gdb.parse_and_eval(f"(unsigned long long)${register}"))
        gdb.execute(f"set ${register} = {value ^ (1 << bit)}")
    except gdb.error:
        # Vector/float registers (e.g. xmm) are changed one byte at a time.
        value = gdb.parse_and_eval(f"${register}")
        field = next((f for f in VECTOR_BYTE_FIELDS if f in value.type.keys()), None)
        if field is None:
            raise RuntimeError(f"Cannot inject into {register} (fields: {value.type.keys()})")
        byte = int(value[field][bit // 8]) ^ (1 << (bit % 8))
        gdb.execute(f"set ${register}.{field}[{bit // 8}] = {byte}")


def main():
    registers = os.environ["TARGET_REGS"].split(",")
    if os.environ.get("DATA_ONLY_MODE") == "1":
        config = get_arch_config()
        registers = ([r for r in registers if r not in config["pointer_blacklist"]]
                     or config["safe_regs_fallback"])

    start_at_function(os.environ["TARGET_FUNC"])
    step_randomly(int(os.environ["MAX_STEPS"]))

    register = random.choice(registers)
    size = gdb.parse_and_eval(f"${register}").type.sizeof * 8
    bit = random.randrange(size)
    flip_register_bit(register, bit)
    print(f"[*] Injected fault into {register} (Size: {size}-bit), flipped bit {bit}")

    continue_and_dump_state()


run_script(main)
