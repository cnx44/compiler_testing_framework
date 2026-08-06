import gdb
import random
import os
import sys

# Tell GDB's Python interpreter to look in the current folder for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gdb_utils import continue_and_dump_state, get_arch_config

gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set verbose off")

def inject_diagnostic_memory():
    target_func = os.environ.get("TARGET_FUNC")
    max_steps_env = os.environ.get("MAX_STEPS")
    data_only_mode = os.environ.get("DATA_ONLY_MODE", "0")
    
    # Pulling the new generalized configuration variables
    payload_ptr = os.environ.get("PAYLOAD_PTR", "")
    length_var = os.environ.get("LENGTH_VAR", "")

    if not target_func or not max_steps_env:
        print("[!] Missing Environment Variables")
        return

    max_steps = int(max_steps_env)
    data_type_size = 4

    out_file = os.environ.get("INFERIOR_OUT", ".inferior_out.txt")
    gdb.execute(f"set args > {out_file}")
    gdb.execute("start")
    gdb.execute("delete")

    # Stop at Instruction 0 to cleanly read the function arguments
    bp = gdb.Breakpoint(target_func)
    gdb.execute("continue")
    bp.delete()

    target_address = None
    strategy_used = "UNKNOWN"

    print(f"\n--- Diagnostic Injection Start (Data Mode: {data_only_mode}) ---")

    # --- Strategy: Config-Defined Dynamic Bounds (Data-Only Mode) ---
    if data_only_mode == "1" and payload_ptr:
        try: # Outer Try Block to catch any extraction errors
            # 1. Evaluate Base Pointer with ABI Fallback
            type_code = None
            try:
                print(f"[DIAG] Evaluating base pointer: {payload_ptr}")
                ptr_val = gdb.parse_and_eval(payload_ptr)
                type_code = ptr_val.type.strip_typedefs().code
                
                # Extract data type size dynamically
                if type_code in (gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_ARRAY):
                    data_type_size = int(ptr_val.type.strip_typedefs().target().sizeof)
                else:
                    data_type_size = int(ptr_val.type.sizeof)
                    
               # Extract base physical address
                if type_code == gdb.TYPE_CODE_PTR:
                    base_addr = int(ptr_val)
                else:
                    if ptr_val.address is None:
                        raise ValueError(f"'{payload_ptr}' has no memory address (likely in a register).")
                    base_addr = int(ptr_val.address)
                    
                # Bulletproof DWARF check: Test if the address is actually readable
                try:
                    gdb.selected_inferior().read_memory(base_addr, 1)
                except gdb.MemoryError:
                    raise ValueError("DWARF pointer points to unmapped/garbage memory.")
                    
            except Exception as e:
                arch_config = get_arch_config()
                arg1_reg = arch_config["arg1_reg"]
                print(f"[DIAG] DWARF corrupted ({e}). Reading Arg 1 directly from ${arg1_reg} register.")
                base_addr = int(gdb.parse_and_eval(f"(unsigned long long)${arg1_reg}"))
                
            print(f"[DIAG] Base address found: {hex(base_addr)}")
            
            # 2. Extract dynamic length with ABI Fallback
            max_elements = 1
            if length_var:
                try:
                    print(f"[DIAG] Evaluating length variable: {length_var}")
                    max_elements = int(gdb.parse_and_eval(length_var))
                    
                    # DWARF corruption check for length (EDDI pack overflow)
                    if max_elements > 1000000 or max_elements < 0:
                        raise ValueError("Corrupted DWARF length detected.")
                except Exception as e:
                    arch_config = get_arch_config()
                    arg3_reg = arch_config["arg3_reg"]
                    print(f"[DIAG] DWARF corrupted ({e}). Reading Arg 3 directly from ${arg3_reg} register.")
                    max_elements = int(gdb.parse_and_eval(f"(unsigned long long)${arg3_reg}"))
            else:
                try:
                    if type_code == gdb.TYPE_CODE_ARRAY:
                        max_elements = int(ptr_val.type.sizeof / data_type_size)
                        print(f"[DIAG] Inferred static array length: {max_elements} elements")
                    else:
                        print("[DIAG] No length_var provided and not a static array. Defaulting to 1 element.")
                        max_elements = 1
                except Exception:
                    print("[DIAG] Failed to infer length. Defaulting to 1 element.")
                    max_elements = 1
                    
            print(f"[DIAG] Payload length: {max_elements} elements")
            
            # Calculate absolute byte boundaries
            payload_byte_size = max_elements * data_type_size
            print(f"[DIAG] Total payload size: {payload_byte_size} bytes")

            # Ensure we have a valid, mapped pointer and enough size to flip 4 bytes
            if base_addr > 0x1000 and payload_byte_size >= 4:
                valid_offsets = list(range(0, payload_byte_size - 3, 4))
                if valid_offsets:
                    offset = random.choice(valid_offsets)
                    target_address = base_addr + offset
                    strategy_used = f"Config Bounds ({payload_ptr} + {offset})"
                    print(f"[DIAG] Successfully calculated target: {hex(target_address)}")
            else:
                print("[DIAG] Invalid pointer or payload size too small.")
        except Exception as e:
            print(f"[DIAG] Exception during dynamic bound extraction: {e}")

    # Random step forward into the execution
    steps = random.randint(1, max_steps)
    print(f"[DIAG] Stepping forward {steps} instructions...")
    gdb.execute(f"nexti {steps}", to_string=True)

    # --- Strategy: Safe Stack Bounds (Fallback) ---
    if target_address is None:
        try:
            rsp_val = int(gdb.parse_and_eval("(unsigned long long)$sp"))
            rbp_val = int(gdb.parse_and_eval("(unsigned long long)$fp"))
            print(f"[DIAG] Fallback triggered. Stack bounds: SP={hex(rsp_val)}, FP={hex(rbp_val)}")
            
            if rbp_val > rsp_val + 8:
                valid_offsets = list(range(0, (rbp_val - rsp_val) - 4, 4))
                if valid_offsets:
                    offset = random.choice(valid_offsets)
                    target_address = rsp_val + offset
                    strategy_used = "Safe Stack Bounds"
                else:
                    target_address = rsp_val
                    strategy_used = "RSP Direct"
            else:
                target_address = rsp_val
                strategy_used = "RSP Direct"
        except Exception as e:
            print(f"[DIAG] Fallback failed: {e}")
            return

    print(f"[DIAG] Final Injection Target: {hex(target_address)} (Strategy: {strategy_used})")

    # Execution: Read, Flip, Write
    inferior = gdb.selected_inferior()
    mem_view = inferior.read_memory(target_address, 4)
    mem_bytes = bytearray(mem_view)

    bit_to_flip = random.randint(0, 31)
    byte_index = bit_to_flip // 8
    bit_index = bit_to_flip % 8

    mem_bytes[byte_index] ^= (1 << bit_index)
    inferior.write_memory(target_address, bytes(mem_bytes))

    print(f"[*] Injected memory successfully.")
    
    # Continue execution and dump the state for the Oracle to read
    continue_and_dump_state()

try:
    inject_diagnostic_memory()
except Exception as e:
    print(f"[FATAL] Uncaught Exception in memory injection: {e}")
finally:
    gdb.execute("quit")