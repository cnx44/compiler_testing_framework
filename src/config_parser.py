import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from lark import Lark, Transformer

DEFAULT_TIMEOUT_MS = 5000



class DataProtectionMode(Enum):
    EDDI = "eddi"
    SEDDI = "seddi"
    FDSC = "fdsc"
    NONE = "none"


class ControlFlowMode(Enum):
    CFCSS = "cfcss"
    RASM = "rasm"
    INTER_RASM = "inter-rasm"
    NONE = "none"


@dataclass(frozen=True)
class RegisterInjection:
    data_only: bool = False


@dataclass(frozen=True)
class MemoryInjection:
    pass


@dataclass(frozen=True)
class GlobalInjection:
    pass


Injection = RegisterInjection | MemoryInjection | GlobalInjection


@dataclass(frozen=True)
class TargetVariable:
    name: str
    length: int | str | None = None
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
    injection: Injection = field(default_factory=RegisterInjection)
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    functions: list[TargetFunction] = field(default_factory=lambda: [TargetFunction("main")])
    compiler_flags: list[str] = field(default_factory=list)
    execution_repetitions: int = 1
    measure_overhead: bool = True
    measurement_repetitions: int | None = None


@dataclass(frozen=True)
class Campaign:
    runs: list[RunnerTest]


class RunnerConfigTransformer(Transformer):
    """Turn the parse tree into dataclasses. Keyword tokens are already
    filtered out by the grammar (``_``-prefixed terminals)."""

    def __init__(self, config_directory: Path):
        super().__init__()
        self.config_directory = config_directory

    WORD = COMPILER_FLAG = str

    def PROGRAM_PATH(self, token):
        return self._resolve(str(token))

    def ESCAPED_STRING(self, token):
        return self._resolve(json.loads(token))

    def _resolve(self, program: str) -> str:
        path = Path(program).expanduser()
        return str((self.config_directory / path).resolve())

    def BOOLEAN(self, token):
        return token == "true"

    def POSITIVE_INT(self, token):
        return int(token)

    def DATA_PROTECTION(self, token):
        return DataProtectionMode(token)

    def CONTROL_FLOW(self, token):
        return ControlFlowMode(token)

    def programs(self, items):
        return items

    def register_mode(self, items):
        return RegisterInjection(data_only=bool(items and items[0]))

    def memory_mode(self, items):
        return MemoryInjection()

    def global_mode(self, items):
        return GlobalInjection()

    def var_def(self, items):
        is_global = items[0] == "@"
        name, *length = items[1:] if is_global else items
        length = length[0] if length else None
        if isinstance(length, str) and length.isdigit():
            length = int(length)
        return TargetVariable(name=name, length=length, is_global=is_global)

    def function_def(self, items):
        return TargetFunction(name=items[0], variables=items[1:])

    # Optional fields become (RunnerTest field, value) pairs.
    def timeout_ms(self, items):
        return "timeout_ms", items[0]

    def execution_repetitions(self, items):
        return "execution_repetitions", items[0]

    def measure_overhead(self, items):
        return "measure_overhead", items[0]

    def measurement_repetitions(self, items):
        return "measurement_repetitions", items[0]

    def compiler_flags(self, items):
        return "compiler_flags", items

    def injection(self, items):
        return "injection", items[0]

    def functions(self, items):
        return "functions", items

    def test(self, items):
        name, programs, data_protection, control_flow, *options = items
        return RunnerTest(name, programs, data_protection, control_flow, **dict(options))

    def campaign(self, items):
        return Campaign(runs=items)


_PARSER = Lark(
    Path(__file__).with_name("grammar.lark").read_text(encoding="utf-8"),
    start="campaign",
    parser="lalr",
)


def parse_config_text(text: str, config_directory: str | Path = ".") -> Campaign:
    """Parse configuration text; relative program paths are resolved against
    ``config_directory``."""
    directory = Path(config_directory).expanduser().resolve()
    return RunnerConfigTransformer(directory).transform(_PARSER.parse(text))


def parse_runner_config(config_path: str | Path) -> Campaign:
    config_path = Path(config_path).expanduser().resolve()
    return parse_config_text(config_path.read_text(encoding="utf-8"), config_path.parent)
