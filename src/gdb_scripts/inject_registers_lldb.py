import lldb
import os
import random
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from lldb_utils import continue_and_dump_state, get_arch_config, run_to_function, selected_frame


def inject_register(debugger):
    target_func = os.environ.get("TARGET_FUNC")
    max_steps_env = os.environ.get("MAX_STEPS")
    target_regs_env = os.environ.get("TARGET_REGS")
    data_only_mode = os.environ.get("DATA_ONLY_MODE", "0")
    if not target_func or not max_steps_env:
        print("[INJECTOR ERROR] Missing step or function environment variables.")
        return
    if not target_regs_env:
        print("[INJECTOR ERROR] No target registers provided by the profiler. Aborting.")
        return

    target, process, _ = run_to_function(debugger, target_func)
    thread = process.GetSelectedThread()
    for _ in range(random.randint(1, int(max_steps_env))):
        thread.StepInstruction(True)
    frame = selected_frame(process)

    registers = target_regs_env.split(",")
    if data_only_mode == "1":
        config = get_arch_config(target)
        registers = [name for name in registers if name not in config["pointer_blacklist"]]
        if not registers:
            registers = config["safe_regs_fallback"]

    name = random.choice(registers)
    register = frame.FindRegister(name)
    if not register.IsValid() or register.GetError().Fail():
        raise ValueError(f"Register '{name}' is unavailable.")
    bits = max(register.GetByteSize() * 8, 1)
    bit = random.randint(0, bits - 1)
    
    error = lldb.SBError()
    data = register.GetData()
    raw = bytearray(data.ReadRawData(error, 0, register.GetByteSize()))
    if error.Fail():
        raise RuntimeError(f"Could not read register '{name}': {error.GetCString()}")
        
    byte_idx = bit // 8
    bit_idx = bit % 8
    raw[byte_idx] ^= (1 << bit_idx)
    
    new_data = lldb.SBData()
    new_data.SetData(error, raw, data.GetByteOrder(), data.GetAddressByteSize())
    register.SetData(new_data, error)
    if error.Fail():
        raise RuntimeError(error.GetCString() or f"Could not write register '{name}'.")
    print(f"[*] Injected fault into {name} (Size: {bits}-bit), flipped bit {bit}")
    continue_and_dump_state(debugger, target, process)


def __lldb_init_module(debugger, internal_dict):
    try:
        inject_register(debugger)
    except Exception as error:
        print(f"[FATAL] Uncaught Exception in register injection: {error}")
    finally:
        debugger.HandleCommand("quit")

