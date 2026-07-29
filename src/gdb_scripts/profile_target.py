import gdb
import os
import re

# Disable GDB output clutter
gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set verbose off")

# A strict set of valid x86_64 general purpose registers in all their size variants
VALID_REGS = {
    "rax", "eax", "ax", "al", "ah",
    "rbx", "ebx", "bx", "bl", "bh",
    "rcx", "ecx", "cx", "cl", "ch",
    "rdx", "edx", "dx", "dl", "dh",
    "rsi", "esi", "si", "sil",
    "rdi", "edi", "di", "dil",
    "rbp", "ebp", "bp", "bpl",
    "rsp", "esp", "sp", "spl",
    "r8", "r8d", "r8w", "r8b",
    "r9", "r9d", "r9w", "r9b",
    "r10", "r10d", "r10w", "r10b",
    "r11", "r11d", "r11w", "r11b",
    "r12", "r12d", "r12w", "r12b",
    "r13", "r13d", "r13w", "r13b",
    "r14", "r14d", "r14w", "r14b",
    "r15", "r15d", "r15w", "r15b"
}

def profile():
    try:
        target_func = os.environ.get("TARGET_FUNC")
        if not target_func:
            print("PROFILED_STEPS=ERROR_NO_FUNC")
            return

        # Route output to avoid cluttering stdout during execution
        gdb.execute("set args > .inferior_out.txt")
        gdb.execute("start")
        gdb.execute("delete")
        gdb.execute(f"break {target_func}")
        gdb.execute("continue")
        gdb.execute("delete")

        # --- Phase A: Extract Exact Used Registers ---
        used_regs = set()
        disassembly = gdb.execute(f"disassemble {target_func}", to_string=True)
        
        # Regex to find all register operands
        matches = re.findall(r'%([a-z0-9]+)', disassembly)
        for reg in matches:
            if reg in VALID_REGS:
                used_regs.add(reg)

        regs_str = ",".join(used_regs)

        # --- Phase B: Count Steps ---
        max_steps = 0
        while True:
            frame = gdb.newest_frame()
            
            if frame.name() != target_func:
                break
                
            # Use nexti to step OVER function calls, staying in the target scope
            gdb.execute("nexti", to_string=True)
            max_steps += 1

        # Output both metrics
        print(f"PROFILED_STEPS={max_steps}")
        print(f"PROFILED_REGS={regs_str}")
        
    except Exception as e:
        print(f"PROFILED_STEPS=ERROR_EXCEPTION_{e}")

profile()
gdb.execute("quit")