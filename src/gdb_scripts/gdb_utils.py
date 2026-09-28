# gdb_utils.py
import gdb
import os
import re


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
    frame = gdb.selected_frame()
    if frame.find_sal().line == return_line:
        # A source breakpoint on the line we already occupy can run past the
        # entire function (notably a one-line main). Stop before its return.
        block = gdb.block_for_pc(int(gdb.parse_and_eval(f"&{entry_func}")))
        instructions = frame.architecture().disassemble(block.start, block.end)
        returns = [instruction["addr"] for instruction in instructions
                   if re.match(r"^(?:ret\w*\b|bx\s+lr\b)", instruction["asm"].strip())]
        if frame.pc() in returns:
            return
        if not returns:
            raise RuntimeError(f"Could not locate a return instruction for '{entry_func}'.")
        breakpoints = [gdb.Breakpoint(f"*{address:#x}") for address in returns]
        try:
            gdb.execute("continue")
        finally:
            for breakpoint in breakpoints:
                breakpoint.delete()
        return
    breakpoint = gdb.Breakpoint(str(return_line))
    try:
        gdb.execute("continue")
    finally:
        breakpoint.delete()


def print_value(name, expression):
    """Print one ``name = value`` line of the state dump."""
    gdb.execute(f'printf "{name} = "')
    try:
        gdb.execute(f"output {expression}")
    except gdb.error as e:
        print(f"<error: {e}>")
    gdb.execute('printf "\\n"')


def dump_requested_variables(vars_to_check):
    """Print the configured variables, then the VAR_LENGTHS arrays.

    The arrays go in a second @@LOCALS@@ section so that their full contents
    replace the raw pointer values printed by ``info locals``.
    """
    gdb.execute('printf "@@GLOBALS@@\\n"')
    for var_name in vars_to_check:
        print_value(var_name, var_name)

    var_lengths_str = os.environ.get("VAR_LENGTHS", "")
    if var_lengths_str:
        gdb.execute('printf "@@LOCALS@@\\n"')
        for pair in var_lengths_str.split(","):
            if ":" in pair:
                var_name, var_len = pair.split(":", 1)
                var_name, var_len = var_name.strip(), var_len.strip()
                # *ptr@len prints the pointer as an array of that length
                print_value(var_name, f"*{var_name}@{var_len}")


def start_at_function(target_func):
    """Start the inferior and stop at the entry of ``target_func``."""
    gdb.execute("start")
    gdb.execute("delete")
    if gdb.newest_frame().name() != target_func:
        bp = gdb.Breakpoint(target_func)
        gdb.execute("continue")
        bp.delete()


def continue_and_dump_state():
    """Continues to the configured function return and dumps its state."""
    entry_func = os.environ.get("ENTRY_FUNC", "main")
    vars_to_check_str = os.environ.get("VARS_TO_CHECK", "")
    vars_to_check = [v.strip() for v in vars_to_check_str.split(",") if v.strip()]

    try:
        continue_to_function_return(entry_func)
    except gdb.error as e:
        print(f"Program received signal: {e}") # Print the error so the injector can classify it as a CRASH

    try:
        gdb.execute('call (int)fflush(0)')
    except gdb.error:
        pass
        
    try:
        gdb.execute('printf "@@ARGS@@\\n"')
        gdb.execute("info args")
        
        gdb.execute('printf "@@LOCALS@@\\n"')
        gdb.execute("info locals")
        
        dump_requested_variables(vars_to_check)

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
                "rsi", "esi", "si", "sil",
                "rip", "eip", "ip"
            },
            "safe_regs_fallback": ["eax", "edx", "ecx"]
        }
    elif "aarch64" in arch_name:
        return {
            "arg1_reg": "x0",
            "arg3_reg": "x2",
            "pointer_blacklist": {"sp", "wsp", "x29", "w29", "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7", "pc", "lr", "x30"},
            "safe_regs_fallback": ["x8", "x9", "x10"]
        }
    elif "arm" in arch_name:
        return {
            "arg1_reg": "r0",
            "arg3_reg": "r2",
            "pointer_blacklist": {"sp", "r13", "fp", "r11", "r0", "r1", "r2", "r3", "pc", "r15", "lr", "r14"},
            "safe_regs_fallback": ["r4", "r5", "r6"]
        }
    elif "riscv" in arch_name:
        return {
            "arg1_reg": "a0",
            "arg3_reg": "a2",
            "pointer_blacklist": {"sp", "s0", "fp", "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7", "pc", "ra"},
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
