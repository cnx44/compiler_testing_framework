import lldb
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from lldb_utils import continue_to_function_return, run_to_function


def __lldb_init_module(debugger, internal_dict):
    target_func = os.environ.get("TARGET_FUNC", "main")
    vars_to_check_str = os.environ.get("VARS_TO_CHECK", "")

    vars_to_check = [v.strip() for v in vars_to_check_str.split(",") if v.strip()]

    # Make command execution synchronous
    debugger.SetAsync(False)
    target, process, _ = run_to_function(debugger, target_func)
    continue_to_function_return(target, process)
    
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
