import gdb
import random
import os
import sys

# Tell GDB's Python interpreter to look in the current folder for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import continue_and_dump_state, start_at_function, get_arch_config

gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set verbose off")

def get_max_bit(reg):
    # Dynamically returns the maximum bit index based on the register's architectural size.
    try:
        reg_size_bytes = gdb.parse_and_eval(f"${reg}").type.sizeof
        return (reg_size_bytes * 8) - 1
    except gdb.error:
        # Fallback just in case
        return 63

def inject_smart_register():
    try:
        target_func = os.environ.get("TARGET_FUNC")
        max_steps_env = os.environ.get("MAX_STEPS")
        target_regs_env = os.environ.get("TARGET_REGS")
        data_only_mode = os.environ.get("DATA_ONLY_MODE", "0")

        # Explicit error handling for missing variables
        if not target_func or not max_steps_env:
            print("\n[INJECTOR ERROR] Missing step or function environment variables.\n")
            return

        if not target_regs_env:
            print("\n[INJECTOR ERROR] No target registers provided by the profiler. Aborting.\n")
            return

        max_steps = int(max_steps_env)
        profiled_regs = target_regs_env.split(",")

        out_file = os.environ.get("INFERIOR_OUT", ".inferior_out.txt")
        gdb.execute(f"set args > {out_file}")
        start_at_function(target_func)

        steps = random.randint(1, max_steps)
        gdb.execute(f"nexti {steps}", to_string=True)

        # Apply the Data-Only filter if the flag was passed in Bash
        if data_only_mode == "1":
            arch_config = get_arch_config()
            pointer_blacklist = arch_config["pointer_blacklist"]
            safe_regs = [reg for reg in profiled_regs if reg not in pointer_blacklist]
            
            # Fallback in the rare case the compiler only used pointers
            if not safe_regs:
                safe_regs = arch_config["safe_regs_fallback"]
            target = random.choice(safe_regs)
        else:
            # Use the full list, including pointers
            target = random.choice(profiled_regs)

        max_bit = get_max_bit(target)

        # Inject strictly within the architectural boundaries
        bit = random.randint(0, max_bit)
        
        try:
            val = int(gdb.parse_and_eval(f"(unsigned long long)${target}"))
            new_val = val ^ (1 << bit)
            gdb.execute(f"set ${target} = {new_val}")
        except gdb.error:
            # Fallback for vector/float registers (e.g. xmm)
            byte_idx = bit // 8
            bit_idx = bit % 8
            val_obj = gdb.parse_and_eval(f"${target}")
            keys = val_obj.type.keys()
            
            array_field = None
            for k in ['v16_int8', 'v8_int8', 'v32_int8', 'v64_int8', 'b']:
                if k in keys:
                    array_field = k
                    break
                    
            if array_field:
                old_byte = int(val_obj[array_field][byte_idx])
                new_byte = old_byte ^ (1 << bit_idx)
                gdb.execute(f"set ${target}.{array_field}[{byte_idx}] = {new_byte}")
            else:
                raise Exception(f"Cannot perform byte-level injection on {target} (type keys: {keys})")

        print(f"[*] Injected fault into {target} (Size: {max_bit + 1}-bit), flipped bit {bit}")

        # Continue execution and dump the state for the Oracle to read
        continue_and_dump_state()

    except Exception as e:
        print(f"[FATAL] Uncaught Exception in register injection: {e}")

inject_smart_register()
gdb.execute("quit")