from dataclasses import dataclass
from pathlib import Path
@dataclass
class Environment:
    aspis_path: str | Path = ""
    vanilla_compiler: str = "clang"
    llvm_path: str | Path = ""
