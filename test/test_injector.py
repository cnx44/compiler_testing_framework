import os
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch

from src.injector import (
    BaseInjector,
    MemoryInjector,
    NormalExecution,
    RegisterInjector,
)
from src.models import ExecutionState, TerminationEvent


class BaseInjectorTest(unittest.TestCase):
    def _injector(self, source: str):
        return NormalExecution("/tmp/program", source, "main")

    def test_is_abstract(self):
        with self.assertRaises(TypeError):
            BaseInjector("program", "main.c", "main")

    @patch("src.injector.subprocess.run")
    def test_profile_target_parses_steps_and_registers(self, run_process):
        run_process.return_value = subprocess.CompletedProcess(
            [],
            0,
            "noise\nPROFILED_STEPS=42\nPROFILED_REGS=rax,rbx\n",
            "",
        )
        injector = NormalExecution("/tmp/program", "/tmp/main.c", "target")

        profile = injector.profile_target()

        self.assertEqual(
            profile,
            {"MAX_STEPS": "42", "TARGET_REGS": "rax,rbx"},
        )
        command = run_process.call_args.args[0]
        self.assertEqual(command[:5], ["gdb", "-batch", "-q", "-x", str(injector.base_dir / "profile_target.py")])
        self.assertEqual(command[-2:], ["--args", "/tmp/program"])
        self.assertEqual(run_process.call_args.kwargs["env"]["TARGET_FUNC"], "target")

    def test_main_return_line_uses_last_return(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".c", encoding="utf-8"
        ) as source:
            source.write("int helper() { return 1; }\nint main() {\nreturn 0;\n}\n")
            source.flush()
            self.assertEqual(self._injector(source.name)._main_return_line(), 3)

    def test_main_return_line_requires_return_statement(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".c", encoding="utf-8"
        ) as source:
            source.write("int main() {}\n")
            source.flush()
            with self.assertRaisesRegex(RuntimeError, "return line"):
                self._injector(source.name)._main_return_line()

    def test_extract_state_parses_output_and_inferior_stdout(self):
        output = """@@ARGS@@
argc = 2
@@LOCALS@@
(int) x = 4
@@REGISTERS@@
rax 0x4
"""
        with tempfile.TemporaryDirectory() as directory:
            inferior_out = Path(directory) / "inferior.txt"
            inferior_out.write_text("program output\n", encoding="utf-8")
            state = self._injector(__file__)._extract_state(
                output, "warning", inferior_out
            )

        self.assertEqual(state.stdout, "program output")
        self.assertEqual(state.stderr, "warning")
        self.assertEqual(state.variables, {"argc": "2", "x": "4"})
        self.assertEqual(state.registers, {"rax": "0x4"})
        self.assertEqual(state.error, "")

    def test_extract_state_records_marker_errors(self):
        state = self._injector(__file__)._extract_state(
            "no snapshot", "", Path("/missing/inferior-output")
        )
        self.assertEqual(state.variables, {})
        self.assertEqual(state.registers, {})
        self.assertIn("Snapshot marker", state.error)

    def test_section_requires_start_marker(self):
        with self.assertRaisesRegex(ValueError, "@@ARGS@@"):
            self._injector(__file__)._section("plain output", "@@ARGS@@")

    def test_run_classifies_all_non_timeout_events(self):
        cases = [
            ("normal", "", 0, TerminationEvent.RETURNED),
            ("Data corruption detected", "", 1, TerminationEvent.ASPIS_DATA),
            ("", "Control-flow signature mismatch", 1, TerminationEvent.ASPIS_CF),
            ("Program received signal SIGSEGV", "", 1, TerminationEvent.CRASHED),
            ("Program terminated with signal SIGILL", "", 1, TerminationEvent.CRASHED),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.c"
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            for stdout, stderr, returncode, expected in cases:
                with self.subTest(event=expected):
                    injector = self._injector(str(source))
                    injector._extract_state = Mock(return_value=ExecutionState())
                    completed = subprocess.CompletedProcess(
                        [], returncode, stdout, stderr
                    )
                    with chdir(directory), patch(
                        "src.injector.subprocess.run", return_value=completed
                    ):
                        result = injector.run(["arg"])
                    self.assertIs(result.event, expected)
                    command = injector._extract_state.call_args
                    self.assertEqual(command.args[0:2], (stdout, stderr))

    def test_run_classifies_timeout_and_decodes_partial_output(self):
        timeout = subprocess.TimeoutExpired(
            cmd=["gdb"], timeout=5, output=b"partial out", stderr=b"partial err"
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.c"
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            injector = self._injector(str(source))
            injector._extract_state = Mock(return_value=ExecutionState())
            with chdir(directory), patch(
                "src.injector.subprocess.run", side_effect=timeout
            ):
                result = injector.run()

        self.assertIs(result.event, TerminationEvent.TIMEOUT)
        injector._extract_state.assert_called_once()
        self.assertEqual(
            injector._extract_state.call_args.args[:2],
            ("partial out", "partial err"),
        )

    @patch("src.injector.subprocess.run")
    def test_run_builds_gdb_command_and_routing_environment(self, run_process):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.c"
            source.write_text("line 1\nreturn 0;\n", encoding="utf-8")
            injector = self._injector(str(source))
            injector._extract_state = Mock(return_value=ExecutionState())
            with chdir(directory):
                injector.run(["one", "two"])

        call_args = run_process.call_args
        self.assertEqual(
            call_args.args[0],
            [
                "gdb",
                "--batch",
                "-q",
                "-x",
                injector.gdb_script,
                "--args",
                "/tmp/program",
                "one",
                "two",
            ],
        )
        self.assertEqual(call_args.kwargs["env"]["RETURN_LINE"], "2")
        self.assertEqual(call_args.kwargs["env"]["SRC_FILE"], "main.c")
        self.assertEqual(call_args.kwargs["timeout"], 5)


class InjectorImplementationsTest(unittest.TestCase):
    def test_register_injector_setup(self):
        injector = RegisterInjector("program", "main.c", "work", data_only=True)
        injector.profile_target = Mock(
            return_value={"MAX_STEPS": "20", "TARGET_REGS": "rax,rbx"}
        )
        self.assertEqual(
            injector.setup(),
            {
                "TARGET_FUNC": "work",
                "MAX_STEPS": "20",
                "TARGET_REGS": "rax,rbx",
                "DATA_ONLY_MODE": "1",
            },
        )
        self.assertTrue(injector.gdb_script.endswith("gdb_scripts/inject_registers.py"))

    def test_register_injector_requires_complete_profile(self):
        injector = RegisterInjector("program", "main.c", "work")
        for profile in ({}, {"MAX_STEPS": "1"}, {"TARGET_REGS": "rax"}):
            with self.subTest(profile=profile):
                injector.profile_target = Mock(return_value=profile)
                with self.assertRaisesRegex(RuntimeError, "Profiling failed"):
                    injector.setup()

    def test_memory_injector_setup(self):
        injector = MemoryInjector(
            "program",
            "main.c",
            "work",
            payload_ptr="buffer",
            length_var="size",
            data_type_size="8",
            data_only=True,
        )
        injector.profile_target = Mock(return_value={"MAX_STEPS": "12"})
        self.assertEqual(
            injector.setup(),
            {
                "TARGET_FUNC": "work",
                "MAX_STEPS": "12",
                "PAYLOAD_PTR": "buffer",
                "LENGTH_VAR": "size",
                "DATA_TYPE_SIZE": "8",
                "DATA_ONLY_MODE": "1",
            },
        )
        self.assertTrue(injector.gdb_script.endswith("gdb_scripts/inject_memory.py"))

    def test_memory_injector_requires_profiled_steps(self):
        injector = MemoryInjector("program", "main.c", "work")
        injector.profile_target = Mock(return_value={})
        with self.assertRaisesRegex(RuntimeError, "Profiling failed"):
            injector.setup()

    def test_normal_execution_has_no_injection_environment(self):
        injector = NormalExecution("program", "main.c", "main")
        self.assertEqual(injector.setup(), {})
        self.assertTrue(
            injector.gdb_script.endswith("gdb_scripts/extract_baseline_state.py")
        )


if __name__ == "__main__":
    unittest.main()
