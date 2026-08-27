"""Utilities used by the interactive configuration editor.

Keeping serialization outside the curses application makes it possible to test
that the UI always writes a configuration accepted by :mod:`config_parser`.
"""

from __future__ import annotations

from src.config_parser import (
    Campaign,
    ControlFlowMode,
    DataProtectionMode,
    GlobalInjection,
    MemoryInjection,
    RegisterInjection,
    RunnerTest,
)


def campaign_to_text(campaign: Campaign) -> str:
    """Return the canonical configuration language representation of a campaign."""
    return "\n".join(_test_to_lines(run) for run in campaign.runs) + "\n"


def _test_to_lines(run: RunnerTest) -> str:
    lines = [
        f"name: {run.name}",
        f"programs: [{', '.join(run.programs)}]",
        f"data_protection: {_data_mode(run.data_protection)}",
        f"control_flow_protection: {_control_mode(run.control_flow_protection)}",
    ]
    if run.timeout_ms != 5000:
        lines.append(f"timeout_ms: {run.timeout_ms}")
    if run.execution_repetitions != 1:
        lines.append(f"execution_repetitions: {run.execution_repetitions}")
    if run.compiler_flags:
        lines.append(f"compiler_flags: [{', '.join(run.compiler_flags)}]")
    lines.append(f"injection: {_injection(run.injection)}")
    if run.functions:
        lines.append(f"functions: [{', '.join(_function(item) for item in run.functions)}]")
    return "\n".join(lines)


def _data_mode(mode: DataProtectionMode) -> str:
    return mode.name.lower()


def _control_mode(mode: ControlFlowMode) -> str:
    return mode.name.lower().replace("_", "-")


def _injection(injection: RegisterInjection | MemoryInjection | GlobalInjection) -> str:
    if isinstance(injection, RegisterInjection):
        return f"register data_only: {'true' if injection.data_only else 'false'}"
    if isinstance(injection, MemoryInjection):
        return "memory"
    if isinstance(injection, GlobalInjection):
        return f"global target_global: {injection.target_global}" if injection.target_global else "global"
    raise TypeError(f"Unsupported injection: {type(injection)!r}")


def _function(function) -> str:
    if not function.variables:
        return function.name
    variables = ", ".join(
        variable.name if variable.length is None else f"{variable.name}:{variable.length}"
        for variable in function.variables
    )
    return f"{function.name}({variables})"
