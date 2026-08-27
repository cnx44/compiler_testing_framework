import lldb
import os
import shlex


def run_to_function(debugger, function_name):
    """Launch the configured LLDB target and stop at ``function_name``."""
    debugger.SetAsync(False)
    target = debugger.GetSelectedTarget()
    out_file = os.environ.get("INFERIOR_OUT", ".inferior_out.txt")
    debugger.HandleCommand(
        f"settings set target.output-path {shlex.quote(out_file)}"
    )
    breakpoint = target.BreakpointCreateByName(function_name)
    if not breakpoint.IsValid() or breakpoint.GetNumLocations() == 0:
        raise RuntimeError(f"Function '{function_name}' was not found.")

    debugger.HandleCommand("run")
    process = target.GetProcess()
    if not process.IsValid() or process.GetState() != lldb.eStateStopped:
        raise RuntimeError("LLDB could not stop the target process.")
    target.BreakpointDelete(breakpoint.GetID())
    return target, process, process.GetSelectedThread().GetFrameAtIndex(0)


def selected_frame(process):
    thread = process.GetSelectedThread()
    if not thread.IsValid() or thread.GetNumFrames() == 0:
        raise RuntimeError("No LLDB frame is selected.")
    return thread.GetFrameAtIndex(0)


def evaluate(frame, expression):
    value = frame.EvaluateExpression(expression)
    if not value.IsValid() or value.GetError().Fail():
        message = value.GetError().GetCString() if value.IsValid() else "invalid value"
        raise ValueError(f"Could not evaluate '{expression}': {message}")
    return value


def register_value(frame, name):
    value = frame.FindRegister(name)
    if not value.IsValid() or value.GetError().Fail():
        raise ValueError(f"Register '{name}' is unavailable.")
    return value.GetValueAsUnsigned()


def get_arch_config(target):
    triple = (target.GetTriple() or "").lower()
    if "x86_64" in triple or "x86-64" in triple:
        return {
            "arg1_reg": "rdi",
            "arg3_reg": "rdx",
            "sp_reg": "rsp",
            "fp_reg": "rbp",
            "pointer_blacklist": {
                "rsp", "esp", "sp", "spl", "rbp", "ebp", "bp", "bpl",
                "rdi", "edi", "di", "dil", "rsi", "esi", "si", "sil",
            },
            "safe_regs_fallback": ["eax", "edx", "ecx"],
        }
    if "arm64" in triple or "aarch64" in triple:
        return {
            "arg1_reg": "x0",
            "arg3_reg": "x2",
            "sp_reg": "sp",
            "fp_reg": "x29",
            "pointer_blacklist": {
                "sp", "wsp", "x29", "w29", "x0", "x1", "x2", "x3",
                "x4", "x5", "x6", "x7",
            },
            "safe_regs_fallback": ["x8", "x9", "x10"],
        }
    if "arm" in triple:
        return {
            "arg1_reg": "r0",
            "arg3_reg": "r2",
            "sp_reg": "sp",
            "fp_reg": "r11",
            "pointer_blacklist": {"sp", "r13", "fp", "r11", "r0", "r1", "r2", "r3"},
            "safe_regs_fallback": ["r4", "r5", "r6"],
        }
    if "riscv" in triple:
        return {
            "arg1_reg": "a0",
            "arg3_reg": "a2",
            "sp_reg": "sp",
            "fp_reg": "s0",
            "pointer_blacklist": {"sp", "s0", "fp", "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7"},
            "safe_regs_fallback": ["t0", "t1", "t2"],
        }
    return {
        "arg1_reg": "r0",
        "arg3_reg": "r2",
        "sp_reg": "sp",
        "fp_reg": "fp",
        "pointer_blacklist": {"sp", "fp"},
        "safe_regs_fallback": ["r0"],
    }


def continue_to_function_return(target, process):
    """Stop near the end of the current function while its frame is active."""
    try:
        frame = selected_frame(process)
        function = frame.GetFunction()
        lines = []
        if function.IsValid():
            instructions = function.GetInstructions(target)
            for index in range(instructions.GetSize()):
                line_entry = instructions.GetInstructionAtIndex(index).GetAddress().GetLineEntry()
                if line_entry.IsValid() and line_entry.GetLine() > 0:
                    lines.append(line_entry.GetLine())
        if lines:
            source = frame.GetLineEntry().GetFileSpec().GetFilename()
            breakpoint = target.BreakpointCreateByLocation(source, max(lines))
            process.Continue()
            if breakpoint.IsValid():
                target.BreakpointDelete(breakpoint.GetID())
    except Exception:
        pass


def continue_and_dump_state(debugger, target, process):
    """Continue to the current function return and emit portable markers."""
    continue_to_function_return(target, process)

    print("@@ARGS@@")
    debugger.HandleCommand("frame variable")
    print("@@LOCALS@@")
    print("@@REGISTERS@@")
    debugger.HandleCommand("register read")
