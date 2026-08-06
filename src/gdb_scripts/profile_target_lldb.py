import lldb
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from lldb_utils import run_to_function, selected_frame


def _general_register_names(frame):
    names = set()
    register_sets = frame.GetRegisters()
    for set_index in range(register_sets.GetSize()):
        register_set = register_sets.GetValueAtIndex(set_index)
        set_name = (register_set.GetName() or "").lower()
        if "general" not in set_name:
            continue
        for register_index in range(register_set.GetNumChildren()):
            name = register_set.GetChildAtIndex(register_index).GetName()
            if name:
                names.add(name)
    return names


def profile(debugger):
    target_func = os.environ.get("TARGET_FUNC")
    if not target_func:
        print("PROFILED_STEPS=ERROR_NO_FUNC")
        return

    try:
        target, process, frame = run_to_function(debugger, target_func)
        valid_registers = _general_register_names(frame)
        used_registers = set()
        function = frame.GetFunction()
        if function.IsValid():
            instructions = function.GetInstructions(target)
            for index in range(instructions.GetSize()):
                instruction = instructions.GetInstructionAtIndex(index)
                text = f"{instruction.GetMnemonic(target)} {instruction.GetOperands(target)}"
                used_registers.update(
                    word for word in re.findall(r"\b[A-Za-z][A-Za-z0-9]*\b", text)
                    if word in valid_registers
                )

        steps = 0
        thread = process.GetSelectedThread()
        while process.GetState() == lldb.eStateStopped:
            current_frame = selected_frame(process)
            if current_frame.GetFunctionName() != target_func:
                break
            thread.StepInstruction(True)
            steps += 1
            if steps >= 1_000_000:
                raise RuntimeError("instruction profiling limit exceeded")

        print(f"PROFILED_STEPS={steps}")
        print(f"PROFILED_REGS={','.join(sorted(used_registers))}")
    except Exception as error:
        print(f"PROFILED_STEPS=ERROR_EXCEPTION_{error}")


def __lldb_init_module(debugger, internal_dict):
    debugger.SetAsync(False)
    profile(debugger)
    debugger.HandleCommand("quit")

