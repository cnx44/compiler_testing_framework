import gdb
import os
import re

# Disable GDB output clutter
gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set verbose off")


def get_end_line(entry_func: str) -> int:
    try:
        sym = gdb.lookup_global_symbol(entry_func)
        if sym is None and hasattr(gdb, "lookup_static_symbol"):
            sym = gdb.lookup_static_symbol(entry_func)
        
        if sym and sym.value():
            start_pc = int(sym.value().address)
            # Find the end of the function block
            block = gdb.block_for_pc(start_pc)
            while block and not block.function:
                block = block.superblock
            if not block:
                return -1
            end_pc = block.end
            
            lines = set()
            if sym.symtab:
                for entry in sym.symtab.linetable():
                    if start_pc <= entry.pc < end_pc:
                        lines.add(entry.line)
            if lines:
                return max(lines)
    except Exception:
        pass
    return -1

def profile():
    try:
        target_func = os.environ.get("TARGET_FUNC")
        if not target_func:
            print("PROFILED_STEPS=ERROR_NO_FUNC")
            return

        # Route output to avoid cluttering stdout during execution
        out_file = os.environ.get("INFERIOR_OUT", ".inferior_out.txt")
        gdb.execute(f"set args > {out_file}")
        gdb.execute("start")
        gdb.execute("delete")
        if gdb.newest_frame().name() != target_func:
            gdb.execute(f"break {target_func}")
            gdb.execute("continue")
            gdb.execute("delete")

        # --- Phase A: Extract Exact Used Registers ---
        used_regs = set()
        disassembly = gdb.execute(f"disassemble {target_func}", to_string=True)
        
        # Regex to find all potential words that could be registers
        matches = re.findall(r'\b([a-zA-Z0-9_]+)\b', disassembly)
        for reg in matches:
            try:
                # If GDB can evaluate it, it's a valid register or pseudo-register
                val = gdb.parse_and_eval(f"${reg}")
                if val.type.code != gdb.TYPE_CODE_VOID:
                    used_regs.add(reg)
            except gdb.error:
                pass

        regs_str = ",".join(used_regs)

        # --- Phase B: Count Steps ---
        max_steps = 0
        return_line = get_end_line(target_func)
        while True:
            try:
                frame = gdb.newest_frame()
                if frame.name() != target_func:
                    break
                
                sal = frame.find_sal()
                if sal and sal.line == return_line:
                    break
                
                # Use nexti to step OVER function calls, staying in the target scope
                gdb.execute("nexti", to_string=True)
                max_steps += 1
            except gdb.error:
                break

        # Ensure max_steps is at least 0, but subtract 1 to avoid stepping onto the return line
        safe_steps = max(0, max_steps - 1)
        print(f"PROFILED_STEPS={safe_steps}")
        print(f"PROFILED_REGS={regs_str}")
        
    except Exception as e:
        print(f"PROFILED_STEPS=ERROR_EXCEPTION_{e}")

profile()
gdb.execute("quit")