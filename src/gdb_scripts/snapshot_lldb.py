import lldb
import os
import sys

def __lldb_init_module(debugger, internal_dict):
    target_src = os.environ.get("TARGET_SRC")
    return_line = os.environ.get("RETURN_LINE")
    vars_to_check_str = os.environ.get("VARS_TO_CHECK", "")
    
    if not target_src or not return_line:
        print("Error: TARGET_SRC and RETURN_LINE environment variables are required.")
        sys.exit(1)

    vars_to_check = [v.strip() for v in vars_to_check_str.split(",") if v.strip()]
    
    source_name = os.path.basename(target_src)

    # Make command execution synchronous
    debugger.SetAsync(False)
    
    debugger.HandleCommand(f"breakpoint set --file {source_name} --line {return_line}")
    debugger.HandleCommand("run")
    
    print("@@ARGS@@")
    debugger.HandleCommand("frame variable")
    
    print("@@LOCALS@@")
    
    print("@@GLOBALS@@")
    for var_name in vars_to_check:
        debugger.HandleCommand(f"target variable {var_name}")
        
    print("@@REGISTERS@@")
    debugger.HandleCommand("register read")
    
    # Clean exit
    debugger.HandleCommand("quit")
