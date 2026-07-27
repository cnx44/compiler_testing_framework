from dataclasses import dataclass

@dataclass()
class Envoirment:
    aspis_path: str
    vanilla_compiler: str

    def __init__(self):
        self.aspis_path         = ""
        self.vanilla_compiler   = ""

