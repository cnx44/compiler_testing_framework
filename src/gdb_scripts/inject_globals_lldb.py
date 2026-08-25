import lldb
import os
import random
import sys
import re

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from lldb_utils import continue_and_dump_state, run_to_function

def get_all_globals(debugger):
    """Parses 'target variable' output to find global variables."""
    res = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand("target variable", res)
    
    if not res.Succeeded():
        return []
        
    candidates = []
    for line in res.GetOutput().splitlines():
        line = line.strip()
        if not line or line.startswith("Global variables"):
            continue
            
        # Example output lines:
        # (int) my_global = 123
        # (float [10]) my_array = { ... }
        # Extract the variable name right before the '='
        match = re.search(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        if match:
            candidates.append(match.group(1))
            
    return candidates

def inject_global(debugger):
    target_func = os.environ.get("TARGET_FUNC")
    max_steps_env = os.environ.get("MAX_STEPS")
    target_global = os.environ.get("TARGET_GLOBAL", "").strip()

    if not target_func or not max_steps_env:
        print("[INJECTOR ERROR] Missing step or function environment variables.")
        return

    target, process, _ = run_to_function(debugger, target_func)
    thread = process.GetSelectedThread()
    
    steps = random.randint(1, int(max_steps_env))
    print(f"[DIAG] Stepping forward {steps} instructions...")
    for _ in range(steps):
        thread.StepInstruction(True)

    var_name = target_global
    strategy_used = ""

    if target_global:
        strategy_used = f"Explicit Target ({target_global})"
    else:
        candidates = get_all_globals(debugger)
        if not candidates:
            print("[DIAG] No global variables found to inject.")
            return
        var_name = random.choice(candidates)
        strategy_used = f"Random Global ({var_name})"

    # Find the global variable
    val = target.FindFirstGlobalVariable(var_name)
    if not val.IsValid():
        print(f"[DIAG] Failed to evaluate target '{var_name}'")
        return

    target_addr = val.GetAddress().GetLoadAddress(target)
    if target_addr == lldb.LLDB_INVALID_ADDRESS:
        print(f"[DIAG] Variable '{var_name}' is not in memory.")
        return
        
    data_type_size = val.GetByteSize()
    print(f"[DIAG] Final Injection Target: {hex(target_addr)} (Size: {data_type_size} bytes, Strategy: {strategy_used})")

    # Limit to injecting in the first 8 bytes if the variable is huge (like a large array)
    inject_size = min(data_type_size, 8)
    
    error = lldb.SBError()
    mem_bytes = bytearray(process.ReadMemory(target_addr, inject_size, error))
    if error.Fail():
        print(f"[DIAG] Failed to read memory: {error.GetCString()}")
        return

    bit_to_flip = random.randint(0, (inject_size * 8) - 1)
    byte_index = bit_to_flip // 8
    bit_index = bit_to_flip % 8

    mem_bytes[byte_index] ^= (1 << bit_index)
    process.WriteMemory(target_addr, bytes(mem_bytes), error)
    
    if error.Fail():
        print(f"[DIAG] Failed to write memory: {error.GetCString()}")
        return

    print(f"[*] Injected global variable successfully. Flipped bit {bit_to_flip}")
    continue_and_dump_state(debugger, target, process)

def __lldb_init_module(debugger, internal_dict):
    try:
        inject_global(debugger)
    except Exception as error:
        print(f"[FATAL] Uncaught Exception in global injection: {error}")
    finally:
        debugger.HandleCommand("quit")
