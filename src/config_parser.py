import json
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import cast

from lark import Lark, Transformer

from src.constants import DEFAULT_TIMEOUT_MS


class DataProtectionMode(Enum):
    EDDI    = 0
    SEDDI   = 1
    FDSC    = 2
    NONE    = 4

class ControlFlowMode(Enum):
    CFCSS       = 0
    RASM        = 1
    INTER_RASM  = 2
    NONE        = 4

@dataclass(frozen=True)
class RegisterInjection:
    data_only: bool = False

@dataclass(frozen=True)
class MemoryInjection:
    pass

@dataclass(frozen=True)
class GlobalInjection:
    pass

@dataclass(frozen=True)
class TargetVariable:
    name: str
    length: int | None = None
    is_global: bool = False

@dataclass(frozen=True)
class TargetFunction:
    name: str
    variables: list[TargetVariable] = field(default_factory=list)

@dataclass(frozen=True)
class RunnerTest:
    name: str
    programs: list[str]
    data_protection: DataProtectionMode
    control_flow_protection: ControlFlowMode
    injection: RegisterInjection | MemoryInjection | GlobalInjection = field(default_factory=RegisterInjection)
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    functions: list[TargetFunction] = field(default_factory=list)
    compiler_flags: list[str] = field(default_factory=list)
    execution_repetitions: int = 1


@dataclass(frozen=True)
class Campaign:
    runs: list[RunnerTest]

class RunnerConfigTransformer(Transformer):
    def WORD(self, token):
        return str(token)

    def PROGRAM_PATH(self, token):
        return str(token)

    def ESCAPED_STRING(self, token):
        return json.loads(str(token))
    
    def BOOLEAN(self, token):
        return str(token) == "true"
        
    @staticmethod
    def _token_to_enum(enum_type, token):
        enum_name = str(token).upper().replace("-", "_")
        return enum_type[enum_name]

    def DATA_PROTECTION(self, token):
        return self._token_to_enum(DataProtectionMode, token)

    def CONTROL_FLOW(self, token):
        return self._token_to_enum(ControlFlowMode, token)

    def POSITIVE_INT(self, token):
        return int(token)

    def program(self, items):
        return str(items[0])

    def programs(self, items):
        return list(items)

    def word_list(self, items):
        return list(items)

    def COMPILER_FLAG(self, token):
        return str(token)

    def compiler_flag_list(self, items):
        return list(items)

    def compiler_flags(self, items):
        lists = [item for item in items if isinstance(item, list)]
        return ("compiler_flags", lists[0] if lists else [])

    def execution_repetitions(self, items):
        repetitions = next(item for item in items if isinstance(item, int))
        return ("execution_repetitions", repetitions)

    def timeout(self, items):
        return ("timeout_ms", int(items[1]))

    def register_mode(self, items):
        data_only = items[1] if items else False
        return RegisterInjection(data_only=data_only)
        
    def memory_mode(self, items):
        return MemoryInjection()
        
    def global_mode(self, items):
        return GlobalInjection()
        
    def injection(self, items):
        return ("injection", items[1])

    def var_def(self, items):
        is_global = False
        if items and getattr(items[0], "type", "") == "GLOBAL_MARKER":
            is_global = True
            items = items[1:]
        name = items[0]
        length = items[1] if len(items) > 1 else None
        return TargetVariable(name=name, length=length, is_global=is_global)
        
    def var_list(self, items):
        return list(items)
        
    def function_def(self, items):
        name = items[0]
        variables = items[1] if len(items) > 1 else []
        return TargetFunction(name=name, variables=variables)
        
    def function_list(self, items):
        return list(items)

    def functions(self, items):
        lists = [item for item in items if isinstance(item, list)]
        return ("functions", lists[0] if lists else [])

    def test(self, items):
          values = [
              item for item in items
              if not str(item).endswith(":") and str(item) != "\n"
          ]

          optional = dict(item for item in values[4:] if isinstance(item, tuple))

          return RunnerTest(
              name=values[0],
              programs=values[1],
              data_protection=values[2],
              control_flow_protection=values[3],
              compiler_flags=optional.get("compiler_flags", []),
              execution_repetitions=optional.get("execution_repetitions", 1),
              functions=optional.get("functions", [TargetFunction(name="main")]),
              injection=optional.get("injection", RegisterInjection()),
              timeout_ms=optional.get("timeout_ms", DEFAULT_TIMEOUT_MS),
          )

    def campaign(self, items):
          return Campaign(runs=list(items))

"""Parses a campaign configuration file into a typed Campaign object."""
def parse_runner_config(config_path: str | Path) -> Campaign:
    # Load the grammar and initialize the LALR parser
    grammar_path = Path(__file__).with_name("grammar.lark")
    grammar = grammar_path.read_text(encoding="utf-8")
    parser = Lark(grammar, start="campaign", parser="lalr")


    # Read the configuration source and build the Parse Tree
    config_path = Path(config_path).expanduser().resolve()
    source = config_path.read_text(encoding="utf-8")
    tree = parser.parse(source)


    # Transform the tree into the final domain object
    campaign = cast(Campaign, RunnerConfigTransformer().transform(tree))
    runs = [
        replace(
            run,
            programs=[
                _resolve_program_path(program, config_path.parent)
                for program in run.programs
            ],
        )
        for run in campaign.runs
    ]
    return Campaign(runs=runs)


def _resolve_program_path(program: str, config_directory: Path) -> str:
    path = Path(program).expanduser()
    if not path.is_absolute():
        path = config_directory / path
    return str(path.resolve())
