import unittest

from src.env import Environment
from src.models import ExecutionResult, ExecutionState, TerminationEvent


class EnvironmentTest(unittest.TestCase):
    def test_defaults(self):
        environment = Environment()
        self.assertEqual(environment.aspis_path, "")
        self.assertEqual(environment.vanilla_compiler, "clang")
        self.assertEqual(environment.llvm_path, "")
        self.assertEqual(environment.gdb_executable, "gdb")
        self.assertEqual(environment.lldb_executable, "lldb")
        self.assertEqual(environment.aspis_flags, [])

    def test_default_aspis_flags_are_independent(self):
        first = Environment()
        second = Environment()
        first.aspis_flags.append("--no-cleanup")
        self.assertEqual(second.aspis_flags, [])

    def test_accepts_custom_toolchain_paths(self):
        environment = Environment("/aspis/aspis.sh", "/llvm/clang", "/llvm/bin")
        self.assertEqual(environment.aspis_path, "/aspis/aspis.sh")
        self.assertEqual(environment.vanilla_compiler, "/llvm/clang")
        self.assertEqual(environment.llvm_path, "/llvm/bin")

    def test_accepts_custom_debugger_paths(self):
        environment = Environment(gdb_executable="/opt/gdb-multiarch", lldb_executable="/usr/local/bin/lldb-17")
        self.assertEqual(environment.gdb_executable, "/opt/gdb-multiarch")
        self.assertEqual(environment.lldb_executable, "/usr/local/bin/lldb-17")


class ModelsTest(unittest.TestCase):
    def test_execution_state_defaults_are_independent(self):
        first = ExecutionState()
        second = ExecutionState()
        first.variables["x"] = "1"
        first.registers["rax"] = "0x1"
        self.assertEqual(second.variables, {})
        self.assertEqual(second.registers, {})

    def test_execution_result_keeps_event_and_state(self):
        state = ExecutionState(stdout="hello")
        result = ExecutionResult(TerminationEvent.RETURNED, state)
        self.assertIs(result.event, TerminationEvent.RETURNED)
        self.assertIs(result.state, state)

    def test_all_termination_events_have_stable_string_values(self):
        self.assertEqual(
            {event.value for event in TerminationEvent},
            {
                "RETURNED",
                "CRASHED",
                "TIMEOUT",
                "ASPIS_DATA",
                "ASPIS_CF",
                "INTERNAL_ERROR",
            },
        )


if __name__ == "__main__":
    unittest.main()
