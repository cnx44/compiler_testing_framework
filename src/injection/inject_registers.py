import gdb
import random
import os

gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set verbose off")

def get_max_bit(reg):
    # Dynamically returns the maximum bit index based on the register's architectural size.
    if reg in ["al", "ah", "bl", "bh", "cl", "ch", "dl", "dh", "sil", "dil", "bpl", "spl"] or (reg.startswith("r") and reg.endswith("b")):
        return 7
    elif reg in ["ax", "bx", "cx", "dx", "si", "di", "bp", "sp"] or (reg.startswith("r") and reg.endswith("w")):
        return 15
    elif reg.startswith("e") or (reg.startswith("r") and reg.endswith("d")):
        return 31
    else:
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

        gdb.execute("set args > .inferior_out.txt")
        gdb.execute("start")
        gdb.execute("delete")

        bp = gdb.Breakpoint(target_func)
        gdb.execute("continue")
        bp.delete()

        steps = random.randint(1, max_steps)
        gdb.execute(f"stepi {steps}", to_string=True)

        # Apply the Data-Only filter if the flag was passed in Bash
        if data_only_mode == "1":
            pointer_blacklist = {
                "rsp", "esp", "sp", "spl",
                "rbp", "ebp", "bp", "bpl",
                "rdi", "edi", "di", "dil",
                "rsi", "esi", "si", "sil"
            }
            safe_regs = [reg for reg in profiled_regs if reg not in pointer_blacklist]
            
            # Fallback in the rare case the compiler only used pointers
            if not safe_regs:
                safe_regs = ["eax", "edx", "ecx"]
            target = random.choice(safe_regs)
        else:
            # Use the full list, including pointers
            target = random.choice(profiled_regs)

        max_bit = get_max_bit(target)

        val = int(gdb.parse_and_eval(f"(unsigned long long)${target}"))
        
        # Inject strictly within the architectural boundaries
        bit = random.randint(0, max_bit)
        new_val = val ^ (1 << bit)

        gdb.execute(f"set ${target} = {new_val}")
        print(f"[*] Injected fault into {target} (Size: {max_bit + 1}-bit), flipped bit {bit}")

        gdb.execute("continue")

    except Exception as e:
        pass

inject_smart_register()
gdb.execute("quit")