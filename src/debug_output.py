"""Parse the marker-delimited dumps printed by the scripts in ``gdb_scripts``.

A dump looks like::

    @@ARGS@@
    n = 3
    @@LOCALS@@
    (int) x = 7
    @@REGISTERS@@
    rax            0x7    7
"""

import re

_VARIABLE_NAME = re.compile(r"^\s*(?:\([^)]*\)\s*)?([A-Za-z_]\w*)$")
_REGISTER_LINE = re.compile(r"^\s*(\S+)\s+(?:=\s+)?(\S+)")


def section(output: str, start: str, end: str | None = None) -> str | None:
    """Return the text between ``start`` and ``end``, or None without ``start``."""
    if start not in output:
        return None
    text = output.split(start, 1)[1]
    if end is not None and end in text:
        text = text.split(end, 1)[0]
    return text.strip()


def parse_variables(text: str, keep_empty: bool = True) -> dict[str, str]:
    variables = {}
    for line in text.splitlines():
        if " = " not in line:
            continue
        name, value = line.split(" = ", 1)
        match = _VARIABLE_NAME.match(name)
        if match and (keep_empty or value.strip()):
            variables[match.group(1)] = value.strip()
    return variables


def parse_registers(text: str) -> dict[str, str]:
    registers = {}
    for line in text.splitlines():
        match = _REGISTER_LINE.match(line)
        if match:
            registers[match.group(1)] = match.group(2)
    return registers
