from dataclasses import dataclass, field
from lark import Lark, Transformer
from enum import Enum
from pathlib import Path
from typing import cast


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
class RunnerTest:
    name: str
    programs: list[str]
    data_protection: DataProtectionMode
    control_flow_protection: ControlFlowMode
    functions: list[str] = field(default_factory=list)
    vars: list[str] = field(default_factory=list)
    compiler_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Campaign:
    runs: list[RunnerTest]

class RunnerConfigTransformer(Transformer):
    def WORD(self, token):
        return str(token)

    def EXT(self, token):
        return str(token)
    
    @staticmethod
    def _token_to_enum(enum_type, token):
        enum_name = str(token).upper().replace("-", "_")
        return enum_type[enum_name]

    def DATA_PROTECTION(self, token):
        return self._token_to_enum(DataProtectionMode, token)

    def CONTROL_FLOW(self, token):
        return self._token_to_enum(ControlFlowMode, token)

    def program(self, items):
        return f"{items[0]}.{items[1]}"

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

    def functions(self, items):
        lists = [item for item in items if isinstance(item, list)]
        return ("functions", lists[0] if lists else [])

    def vars(self, items):
        lists = [item for item in items if isinstance(item, list)]
        return ("vars", lists[0] if lists else [])

    
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
              functions=optional.get("functions", []),
              vars=optional.get("vars", []),
          )

    def campaign(self, items):
          return Campaign(runs=list(items))

"""Parses a campaign configuration file into a typed Campaign object."""
def parse_runner_config(config_path: str) -> Campaign:
    # Load the grammar and initialize the LALR parser
    grammar_path = Path(__file__).with_name("grammar.lark")
    grammar = grammar_path.read_text(encoding="utf-8")
    parser = Lark(grammar, start="campaign", parser="lalr")


    # Read the configuration source and build the Parse Tree
    source = Path(config_path).read_text(encoding="utf-8")
    tree = parser.parse(source)


    # Transform the tree into the final domain object
    campaign = RunnerConfigTransformer().transform(tree)
    return cast(Campaign, campaign)
