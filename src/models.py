from enum import Enum
from dataclasses import dataclass, field
from typing import Dict

class TerminationEvent(Enum):
    RETURNED = "RETURNED"               # Reached the target return line successfully
    CRASHED = "CRASHED"                 # Terminated by an OS signal (e.g., SIGSEGV, SIGILL)
    TIMEOUT = "TIMEOUT"                 # Exceeded the allowed execution time limit
    ASPIS_DATA = "ASPIS_DATA"           # Caught by ASPIS Data Corruption Handler
    ASPIS_CF = "ASPIS_CF"               # Caught by ASPIS Control-Flow Signature Mismatch
    INTERNAL_ERROR = "INTERNAL_ERROR"   # The debugger crashed or failed to extract state

@dataclass
class ExecutionState:
    stdout: str = ""
    stderr: str = ""
    debugger_output: str = ""
    variables: Dict[str, str] = field(default_factory=dict)
    registers: Dict[str, str] = field(default_factory=dict)
    error: str = "" 

@dataclass
class ExecutionResult:
    event: TerminationEvent
    state: ExecutionState