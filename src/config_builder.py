"""Serialize campaigns in the configuration language parsed by config_parser."""

import json

from src.config_parser import (
    Campaign,
    GlobalInjection,
    Injection,
    MemoryInjection,
    RegisterInjection,
    RunnerTest,
    TargetFunction,
)


def config_block(
    name: str,
    programs: list[str],
    data_protection: str,
    control_flow_protection: str,
    timeout_ms: int | str,
    execution_repetitions: int | str,
    injection: str,
    measure_overhead: bool = True,
    measurement_repetitions: int | str | None = None,
    compiler_flags: str = "",
    functions: str = "",
) -> str:
    """Return one test block; ``compiler_flags`` and ``functions`` are the
    comma-separated list contents."""
    lines = [
        f"name: {name}",
        f"programs: [{', '.join(json.dumps(str(program)) for program in programs)}]",
        f"data_protection: {data_protection}",
        f"control_flow_protection: {control_flow_protection}",
        f"timeout_ms: {timeout_ms}",
        f"execution_repetitions: {execution_repetitions}",
    ]
    if not measure_overhead:
        lines.append("measure_overhead: false")
    if measurement_repetitions:
        lines.append(f"measurement_repetitions: {measurement_repetitions}")
    if compiler_flags:
        lines.append(f"compiler_flags: [{compiler_flags}]")
    lines.append(f"injection: {injection}")
    if functions:
        lines.append(f"functions: [{functions}]")
    return "\n".join(lines) + "\n"


def campaign_to_text(campaign: Campaign) -> str:
    if not campaign.runs:
        raise ValueError("A configuration must contain at least one test.")
    return "".join(run_to_text(run) for run in campaign.runs)


def run_to_text(run: RunnerTest) -> str:
    return config_block(
        run.name,
        run.programs,
        run.data_protection.value,
        run.control_flow_protection.value,
        run.timeout_ms,
        run.execution_repetitions,
        injection_text(run.injection),
        run.measure_overhead,
        run.measurement_repetitions,
        ", ".join(run.compiler_flags),
        ", ".join(function_text(function) for function in run.functions),
    )


def injection_text(injection: Injection) -> str:
    if isinstance(injection, RegisterInjection):
        return f"register data_only: {'true' if injection.data_only else 'false'}"
    if isinstance(injection, MemoryInjection):
        return "memory"
    if isinstance(injection, GlobalInjection):
        return "global"
    raise TypeError(f"Unsupported injection: {type(injection)!r}")


def function_text(function: TargetFunction) -> str:
    """Render ``main`` or ``main(x, @G, buf:4)``."""
    if not function.variables:
        return function.name
    variables = ", ".join(
        ("@" if variable.is_global else "") + variable.name
        + ("" if variable.length is None else f":{variable.length}")
        for variable in function.variables
    )
    return f"{function.name}({variables})"
