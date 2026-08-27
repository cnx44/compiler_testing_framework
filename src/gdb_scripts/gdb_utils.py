# gdb_utils.py
import gdb
import os


def get_end_line(entry_func="main"):
    """Returns the maximum line number belonging to the entry function using DWARF line tables."""
    try:
        func_addr = int(gdb.parse_and_eval(f"&{entry_func}"))
        block = gdb.block_for_pc(func_addr)
        if block:
            start_pc = block.start
            end_pc = block.end
            sym = gdb.lookup_global_symbol(entry_func)
            if sym is None and hasattr(gdb, "lookup_static_symbol"):
                sym = gdb.lookup_static_symbol(entry_func)
            lines = set()
            if sym and sym.symtab:
                for entry in sym.symtab.linetable():
                    if start_pc <= entry.pc < end_pc:
                        lines.add(entry.line)
            if lines:
                return max(lines)
    except Exception:
        pass
    return -1


def run_to_function(entry_func="main"):
    """Starts the inferior and stops at the requested function entry."""
    breakpoint = gdb.Breakpoint(entry_func)
    gdb.execute("run")
    breakpoint.delete()


def continue_to_function_return(entry_func="main"):
    """Continues to the final source line while the function frame is active."""
    return_line = get_end_line(entry_func)
    if return_line == -1:
        raise RuntimeError(f"Could not determine return line for '{entry_func}'.")
    breakpoint = gdb.Breakpoint(str(return_line))
    try:
        gdb.execute("continue")
    finally:
        breakpoint.delete()


def continue_and_dump_state():
    """Continues to the configured function return and dumps its state."""
    entry_func = os.environ.get("ENTRY_FUNC", "main")

    try:
        continue_to_function_return(entry_func)
    except gdb.error:
        pass # Continue to dump state even if it crashes
        
    try:
        gdb.execute('printf "@@ARGS@@\\n"')
        gdb.execute("info args")
        
        gdb.execute('printf "@@LOCALS@@\\n"')
        gdb.execute("info locals")
        
        gdb.execute('printf "@@REGISTERS@@\\n"')
        gdb.execute("info registers")
    except gdb.error:
        pass

def get_arch_config():
    """Returns architecture-specific configuration for edge cases."""
    try:
        arch_name = gdb.selected_frame().architecture().name().lower()
    except gdb.error:
        arch_name = "x86-64" # Default if no frame is active
        
    if "x86-64" in arch_name or "i386:x86-64" in arch_name:
        return {
            "arg1_reg": "rdi",
            "arg3_reg": "rdx",
            "pointer_blacklist": {
                "rsp", "esp", "sp", "spl",
                "rbp", "ebp", "bp", "bpl",
                "rdi", "edi", "di", "dil",
                "rsi", "esi", "si", "sil"
            },
            "safe_regs_fallback": ["eax", "edx", "ecx"]
        }
    elif "aarch64" in arch_name:
        return {
            "arg1_reg": "x0",
            "arg3_reg": "x2",
            "pointer_blacklist": {"sp", "wsp", "x29", "w29", "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7"},
            "safe_regs_fallback": ["x8", "x9", "x10"]
        }
    elif "arm" in arch_name:
        return {
            "arg1_reg": "r0",
            "arg3_reg": "r2",
            "pointer_blacklist": {"sp", "r13", "fp", "r11", "r0", "r1", "r2", "r3"},
            "safe_regs_fallback": ["r4", "r5", "r6"]
        }
    elif "riscv" in arch_name:
        return {
            "arg1_reg": "a0",
            "arg3_reg": "a2",
            "pointer_blacklist": {"sp", "s0", "fp", "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7"},
            "safe_regs_fallback": ["t0", "t1", "t2"]
        }
    else:
        # Generic fallback
        return {
            "arg1_reg": "r0",
            "arg3_reg": "r2",
            "pointer_blacklist": {"sp", "fp"},
            "safe_regs_fallback": ["r0"]
        }
