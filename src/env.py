from dataclasses import dataclass

@dataclass
class Envoirment:
    aspis_path: str = ""
    vanilla_compiler: str = "clang"
    llvm_path: str = ""
