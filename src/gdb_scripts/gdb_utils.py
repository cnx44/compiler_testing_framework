# gdb_utils.py
import gdb
import os

def continue_and_dump_state():
    """Continues execution to the return line and dumps the state."""
    src_file = os.environ.get("SRC_FILE")
    return_line = os.environ.get("RETURN_LINE")
    
    if src_file and return_line:
        gdb.execute(f"break {src_file}:{return_line}")
        try:
            gdb.execute("continue")
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
    else:
        try:
            gdb.execute("continue")
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