import gdb
import random
import os
import sys
import re

# Tell GDB's Python interpreter to look in the current folder for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import continue_and_dump_state

gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set verbose off")

def get_all_globals():
    """Parses 'info variables' to find global and static variables defined in the source."""
    target_src = os.environ.get("TARGET_SRC", "")
    output = gdb.execute("info variables", to_string=True)
    candidates = []
    in_source_files = False
    in_target_file = True if not target_src else False
    
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
            
        if line.startswith("All defined variables:"):
            in_source_files = True
            continue
            
        if line.startswith("Non-debugging symbols:"):
            in_source_files = False
            continue
            
        if in_source_files:
            if line.startswith("File "):
                if target_src and target_src in line:
                    in_target_file = True
                else:
                    in_target_file = False
                continue
            
            if not in_target_file:
                continue
                
            # Lines look like: "int my_global;" or "static float my_static[10];"
            # We want to extract the variable name.
            # Using a regex to capture the name just before a semicolon, array bracket, or equal sign
            match = re.search(r'\b([A-Za-z_][A-Za-z0-9_]*)(?:\s*\[.*\])?\s*;', line)
            if match:
                candidates.append(match.group(1))
                
    return candidates

def inject_global():
    target_func = os.environ.get("TARGET_FUNC")
    max_steps_env = os.environ.get("MAX_STEPS")
    target_global = os.environ.get("TARGET_GLOBAL", "").strip()

    if not target_func or not max_steps_env:
        print("[!] Missing Environment Variables")
        return

    max_steps = int(max_steps_env)
    
    out_file = os.environ.get("INFERIOR_OUT", ".inferior_out.txt")
    gdb.execute(f"set args > {out_file}")
    gdb.execute("start")
    gdb.execute("delete")

    if gdb.newest_frame().name() != target_func:
        bp = gdb.Breakpoint(target_func)
        gdb.execute("continue")
        bp.delete()

    steps = random.randint(1, max_steps)
    print(f"[DIAG] Stepping forward {steps} instructions...")
    gdb.execute(f"nexti {steps}", to_string=True)

    inferior = gdb.selected_inferior()
    
    target_addr = None
    data_type_size = 4
    strategy_used = ""
    var_name = target_global

    if target_global:
        strategy_used = f"Explicit Target ({target_global})"
    else:
        candidates = get_all_globals()
        if not candidates:
            print("[DIAG] No global variables found to inject.")
            return
        var_name = random.choice(candidates)
        strategy_used = f"Random Global ({var_name})"
        
    try:
        val = gdb.parse_and_eval(var_name)
        target_addr = int(val.address)
        data_type_size = int(val.type.sizeof)
    except Exception as e:
        print(f"[DIAG] Failed to evaluate target '{var_name}': {e}")
        return

    if target_addr is None or target_addr == 0:
        print("[DIAG] Target address is invalid.")
        return

    print(f"[DIAG] Final Injection Target: {hex(target_addr)} (Size: {data_type_size} bytes, Strategy: {strategy_used})")

    # Limit to injecting in the first 8 bytes if the variable is huge (like a large array)
    inject_size = min(data_type_size, 8)
    
    mem_view = inferior.read_memory(target_addr, inject_size)
    mem_bytes = bytearray(mem_view)

    bit_to_flip = random.randint(0, (inject_size * 8) - 1)
    byte_index = bit_to_flip // 8
    bit_index = bit_to_flip % 8

    mem_bytes[byte_index] ^= (1 << bit_index)
    inferior.write_memory(target_addr, bytes(mem_bytes))

    print(f"[*] Injected global variable successfully. Flipped bit {bit_to_flip}")
    
    continue_and_dump_state()

try:
    inject_global()
except Exception as e:
    print(f"[FATAL] Uncaught Exception in global injection: {e}")
finally:
    gdb.execute("quit")
