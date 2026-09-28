"""Parse the state dump printed by ``gdb_utils.dump_state``::

    @@ARGS@@
    n = 3
    @@LOCALS@@
    x = 7
    @@REGISTERS@@
    rax            0x7    7
"""

import re

_VARIABLE_NAME = re.compile(r"[A-Za-z_]\w*")
_REGISTER_LINE = re.compile(r"^\s*([a-z]\w*)\s+(\S+)")


def parse_dump(output: str) -> tuple[dict[str, str], dict[str, str]] | None:
    """Return ``(variables, registers)``, or None when no register was dumped
    (the program was no longer running)."""
    if "@@ARGS@@" not in output or "@@REGISTERS@@" not in output:
        return None
    variables_text, registers_text = output.split("@@ARGS@@", 1)[1].split("@@REGISTERS@@", 1)

    variables = {}
    for line in variables_text.splitlines():
        name, separator, value = line.partition(" = ")
        # Later lines win: DUMP_VARS arrays replace the pointers of info locals.
        if separator and _VARIABLE_NAME.fullmatch(name.strip()) and value.strip():
            variables[name.strip()] = value.strip()

    registers = {}
    for line in registers_text.splitlines():
        match = _REGISTER_LINE.match(line)
        if match:
            registers[match.group(1)] = match.group(2)
    return (variables, registers) if registers else None
