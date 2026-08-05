import gdb
import os
import re

# Disable GDB output clutter
gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set verbose off")



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
        
        # Dynamically get valid general purpose registers for the current architecture
        arch = gdb.selected_frame().architecture()
        valid_regs = {reg.name for reg in arch.registers('general') if reg.name}
        
        # Regex to find all potential words that could be registers
        matches = re.findall(r'\b([a-zA-Z0-9_]+)\b', disassembly)
        for reg in matches:
            if reg in valid_regs:
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