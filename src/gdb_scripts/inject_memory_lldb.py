import lldb
import os
import random
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from lldb_utils import (
    continue_and_dump_state,
    evaluate,
    get_arch_config,
    register_value,
    run_to_function,
    selected_frame,
)


def inject_memory(debugger):
    target_func = os.environ.get("TARGET_FUNC")
    max_steps_env = os.environ.get("MAX_STEPS")
    data_only_mode = os.environ.get("DATA_ONLY_MODE", "0")
    payload_ptr = os.environ.get("PAYLOAD_PTR", "")
    length_var = os.environ.get("LENGTH_VAR", "")
    if not target_func or not max_steps_env:
        print("[!] Missing Environment Variables")
        return

    target, process, frame = run_to_function(debugger, target_func)
    max_steps = int(max_steps_env)
    data_type_size = 4
    target_address = None
    strategy_used = "UNKNOWN"
    print(f"\n--- Diagnostic Injection Start (Data Mode: {data_only_mode}) ---")

    if data_only_mode == "1" and payload_ptr:
        try:
            value = None
            value_type = None
            try:
                print(f"[DIAG] Evaluating base pointer: {payload_ptr}")
                value = evaluate(frame, payload_ptr)
                value_type = value.GetType()
                if value_type.IsPointerType():
                    data_type_size = value_type.GetPointeeType().GetByteSize()
                    base_address = value.GetValueAsUnsigned()
                else:
                    data_type_size = value_type.GetArrayElementType().GetByteSize() if value_type.IsArrayType() else value_type.GetByteSize()
                    address_value = value.AddressOf()
                    if not address_value.IsValid() or address_value.GetError().Fail():
                        raise ValueError(f"'{payload_ptr}' has no memory address.")
                    base_address = address_value.GetValueAsUnsigned()
                error = lldb.SBError()
                process.ReadMemory(base_address, 1, error)
                if error.Fail():
                    raise ValueError("DWARF pointer points to unmapped memory.")
            except Exception as error:
                config = get_arch_config(target)
                register = config["arg1_reg"]
                print(f"[DIAG] DWARF corrupted ({error}). Reading Arg 1 directly from ${register} register.")
                base_address = register_value(frame, register)

            print(f"[DIAG] Base address found: {hex(base_address)}")
            max_elements = 1
            if length_var:
                try:
                    print(f"[DIAG] Evaluating length variable: {length_var}")
                    max_elements = evaluate(frame, length_var).GetValueAsSigned()
                    if max_elements > 1_000_000 or max_elements < 0:
                        raise ValueError("Corrupted DWARF length detected.")
                except Exception as error:
                    config = get_arch_config(target)
                    register = config["arg3_reg"]
                    print(f"[DIAG] DWARF corrupted ({error}). Reading Arg 3 directly from ${register} register.")
                    max_elements = register_value(frame, register)
            elif value_type is not None and value_type.IsArrayType():
                element_size = value_type.GetArrayElementType().GetByteSize()
                if element_size <= 0:
                    raise ValueError("Could not determine the array element size.")
                max_elements = value_type.GetByteSize() // element_size
                print(f"[DIAG] Inferred static array length: {max_elements} elements")
            else:
                print("[DIAG] No length_var provided and not a static array. Defaulting to 1 element.")

            print(f"[DIAG] Payload length: {max_elements} elements")
            payload_byte_size = max_elements * data_type_size
            print(f"[DIAG] Total payload size: {payload_byte_size} bytes")
            if base_address > 0x1000 and payload_byte_size >= 4:
                offsets = list(range(0, payload_byte_size - 3, 4))
                if offsets:
                    offset = random.choice(offsets)
                    target_address = base_address + offset
                    strategy_used = f"Config Bounds ({payload_ptr} + {offset})"
                    print(f"[DIAG] Successfully calculated target: {hex(target_address)}")
        except Exception as error:
            print(f"[DIAG] Exception during dynamic bound extraction: {error}")

    steps = random.randint(1, max_steps)
    print(f"[DIAG] Stepping forward {steps} instructions...")
    thread = process.GetSelectedThread()
    for _ in range(steps):
        thread.StepInstruction(True)

    if target_address is None:
        try:
            frame = selected_frame(process)
            config = get_arch_config(target)
            stack_pointer = register_value(frame, config["sp_reg"])
            frame_pointer = register_value(frame, config["fp_reg"])
            print(f"[DIAG] Fallback triggered. Stack bounds: SP={hex(stack_pointer)}, FP={hex(frame_pointer)}")
            if frame_pointer > stack_pointer + 8:
                offsets = list(range(0, frame_pointer - stack_pointer - 4, 4))
                target_address = stack_pointer + random.choice(offsets) if offsets else stack_pointer
                strategy_used = "Safe Stack Bounds" if offsets else "SP Direct"
            else:
                target_address = stack_pointer
                strategy_used = "SP Direct"
        except Exception as error:
            print(f"[DIAG] Fallback failed: {error}")
            return

    print(f"[DIAG] Final Injection Target: {hex(target_address)} (Strategy: {strategy_used})")
    error = lldb.SBError()
    memory = bytearray(process.ReadMemory(target_address, 4, error))
    if error.Fail() or len(memory) != 4:
        raise RuntimeError(error.GetCString() or "Could not read injection target.")
    bit = random.randint(0, 31)
    memory[bit // 8] ^= 1 << (bit % 8)
    process.WriteMemory(target_address, bytes(memory), error)
    if error.Fail():
        raise RuntimeError(error.GetCString())
    print("[*] Injected memory successfully.")
    continue_and_dump_state(debugger, target, process)


def __lldb_init_module(debugger, internal_dict):
    try:
        inject_memory(debugger)
    except Exception as error:
        print(f"[FATAL] Uncaught Exception in memory injection: {error}")
    finally:
        debugger.HandleCommand("quit")
