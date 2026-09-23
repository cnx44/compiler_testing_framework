from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Environment:
    aspis_path: str | Path = ""
    vanilla_compiler: str = "clang"
    llvm_path: str | Path = ""
    gdb_executable: str = "gdb"
    aspis_flags: list[str] = field(default_factory=list)
