"""Helpers shared by the scripts that GDB runs (``gdb -x <script>``).

Configuration comes from environment variables set by the Python runner:

- ``TARGET_FUNC``: function whose return is observed;
- ``DUMP_VARS``: extra variables to print, as ``name`` or ``name:length``;
- ``INFERIOR_OUT`` / ``INFERIOR_ERR``: files receiving the program's output.
"""
import os
import random
import re

import gdb

gdb.execute("set pagination off")
gdb.execute("set confirm off")


def function_end_line(function):
    """Return the highest source line of ``function`` (DWARF line table), or -1."""
    try:
        symbol = gdb.lookup_global_symbol(function) or gdb.lookup_static_symbol(function)
        start = int(symbol.value().address)
        block = gdb.block_for_pc(start)
        while block and not block.function:
            block = block.superblock
        lines = {entry.line for entry in symbol.symtab.linetable()
                 if start <= entry.pc < block.end}
        return max(lines)
    except Exception:
        return -1


def start_at_function(function):
    """Run the program, with its output redirected, until ``function`` is entered."""
    out = os.environ.get("INFERIOR_OUT", os.devnull)
    err = os.environ.get("INFERIOR_ERR", os.devnull)
    # Append the redirections to the arguments given with ``gdb --args``.
    gdb.execute(f"set args {gdb.parameter('args') or ''} > {out} 2> {err}")
    gdb.execute("start")
    gdb.execute("delete")
    if gdb.newest_frame().name() != function:
        gdb.Breakpoint(function)
        gdb.execute("continue")
        gdb.execute("delete")


def step_randomly(max_steps):
    """Execute between 1 and ``max_steps`` machine instructions of the function."""
    steps = random.randint(1, max_steps)
    gdb.execute(f"nexti {steps}", to_string=True)
    return steps


def flip_random_bit(address, size):
    """Flip one random bit among the ``size`` bytes at ``address``."""
    inferior = gdb.selected_inferior()
    data = bytearray(inferior.read_memory(address, size))
    bit = random.randrange(size * 8)
    data[bit // 8] ^= 1 << (bit % 8)
    inferior.write_memory(address, bytes(data))
    return bit


def continue_to_function_return(function):
    """Continue to the last source line of ``function`` while its frame is active."""
    return_line = function_end_line(function)
    if return_line == -1:
        raise RuntimeError(f"Could not determine return line for '{function}'.")
    frame = gdb.selected_frame()
    if frame.find_sal().line != return_line:
        addresses = [str(return_line)]
    else:
        # A source breakpoint on the line we already occupy can run past the
        # entire function (notably a one-line main). Stop at its return instead.
        block = gdb.block_for_pc(int(gdb.parse_and_eval(f"&{function}")))
        instructions = frame.architecture().disassemble(block.start, block.end)
        returns = [i["addr"] for i in instructions
                   if re.match(r"^(?:ret\w*\b|bx\s+lr\b)", i["asm"].strip())]
        if frame.pc() in returns:
            return
        if not returns:
            raise RuntimeError(f"Could not locate a return instruction for '{function}'.")
        addresses = [f"*{address:#x}" for address in returns]
    breakpoints = [gdb.Breakpoint(address) for address in addresses]
    try:
        gdb.execute("continue")
    finally:
        for breakpoint in breakpoints:
            breakpoint.delete()


def _try(command):
    try:
        gdb.execute(command)
    except gdb.error as error:
        print(f"<error: {error}>")


def dump_state():
    """Print args, locals, ``DUMP_VARS`` and registers between @@ markers."""
    _try("call (int)fflush(0)")
    print("@@ARGS@@")
    _try("info args")
    print("@@LOCALS@@")
    _try("info locals")
    for item in filter(None, os.environ.get("DUMP_VARS", "").split(",")):
        name, _, length = item.partition(":")
        print(f"{name} = ", end="")
        # *ptr@len prints the pointed memory as an array of that length.
        _try(f"output *{name}@{length}" if length else f"output {name}")
        print()
    print("@@REGISTERS@@")
    _try("info registers")


def continue_and_dump_state():
    """Let the program reach the target function's return, then dump its state."""
    try:
        continue_to_function_return(os.environ["TARGET_FUNC"])
    except gdb.error as error:
        # Printed so that the injector classifies the run as a crash.
        print(f"Program received signal: {error}")
    dump_state()


def get_arch_config():
    """Return architecture-specific registers used by the injectors."""
    try:
        arch_name = gdb.selected_frame().architecture().name().lower()
    except gdb.error:
        arch_name = "x86-64"

    if "x86-64" in arch_name:
        return {
            "arg1_reg": "rdi",
            "arg3_reg": "rdx",
            "pointer_blacklist": {
                "rsp", "esp", "sp", "spl",
                "rbp", "ebp", "bp", "bpl",
                "rdi", "edi", "di", "dil",
                "rsi", "esi", "si", "sil",
                "rip", "eip", "ip",
            },
            "safe_regs_fallback": ["eax", "edx", "ecx"],
        }
    if "aarch64" in arch_name:
        return {
            "arg1_reg": "x0",
            "arg3_reg": "x2",
            "pointer_blacklist": {"sp", "wsp", "x29", "w29", "x0", "x1", "x2", "x3",
                                  "x4", "x5", "x6", "x7", "pc", "lr", "x30"},
            "safe_regs_fallback": ["x8", "x9", "x10"],
        }
    if "arm" in arch_name:
        return {
            "arg1_reg": "r0",
            "arg3_reg": "r2",
            "pointer_blacklist": {"sp", "r13", "fp", "r11", "r0", "r1", "r2", "r3",
                                  "pc", "r15", "lr", "r14"},
            "safe_regs_fallback": ["r4", "r5", "r6"],
        }
    if "riscv" in arch_name:
        return {
            "arg1_reg": "a0",
            "arg3_reg": "a2",
            "pointer_blacklist": {"sp", "s0", "fp", "a0", "a1", "a2", "a3", "a4",
                                  "a5", "a6", "a7", "pc", "ra"},
            "safe_regs_fallback": ["t0", "t1", "t2"],
        }
    return {
        "arg1_reg": "r0",
        "arg3_reg": "r2",
        "pointer_blacklist": {"sp", "fp"},
        "safe_regs_fallback": ["r0"],
    }


def run_script(main):
    """Run ``main`` and quit GDB; uncaught errors become an internal error."""
    try:
        main()
    except Exception as error:
        print(f"[FATAL] Uncaught exception: {error}")
    finally:
        gdb.execute("quit")
