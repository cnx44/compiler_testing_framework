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
    GlobalInjector,
)
from src.models import ExecutionState, TerminationEvent
from src.env import Environment
from src.config_parser import (
    GlobalInjection,
    MemoryInjection,
    RegisterInjection,
    TargetFunction,
    TargetVariable,
)


class BaseInjectorTest(unittest.TestCase):
    def _injector(self, source: str):
        return NormalExecution(
            Environment(), "/tmp/program", source, "main", debugger="gdb"
        )

    def test_is_abstract(self):
        with self.assertRaises(TypeError):
            BaseInjector(Environment(), "program", "main.c", "main")

    @patch("src.injector.subprocess.run")
    def test_profile_target_parses_steps_and_registers(self, run_process):
        run_process.return_value = subprocess.CompletedProcess(
            [],
            0,
            "noise\nPROFILED_STEPS=42\nPROFILED_REGS=rax,rbx\n",
            "",
        )
        injector = NormalExecution(
            Environment(), "/tmp/program", "/tmp/main.c", "target", debugger="gdb"
        )

        profile = injector.profile_target()

        self.assertEqual(
            profile,
            {"MAX_STEPS": "42", "TARGET_REGS": "rax,rbx"},
        )
        command = run_process.call_args.args[0]
        self.assertEqual(command[:5], ["gdb", "--batch", "-q", "-x", str(injector.base_dir / "profile_target.py")])
        self.assertEqual(command[-2:], ["--args", injector.target_bin])
        self.assertEqual(run_process.call_args.kwargs["env"]["TARGET_FUNC"], "target")
        self.assertEqual(run_process.call_args.kwargs["timeout"], 5)

    @patch("src.injector.subprocess.run")
    def test_profile_target_uses_and_reports_configured_timeout(self, run_process):
        run_process.side_effect = subprocess.TimeoutExpired(["gdb"], 1.25)
        injector = NormalExecution(
            Environment(),
            "/tmp/program",
            "/tmp/main.c",
            "target",
            debugger="gdb",
            timeout_ms=1250,
        )

        with self.assertRaisesRegex(RuntimeError, "timed out after 1250 ms"):
            injector.profile_target()

        self.assertEqual(run_process.call_args.kwargs["timeout"], 1.25)

    @patch("src.injector.shutil.which", side_effect=lambda executable: executable)
    @patch("src.injector.subprocess.run")
    def test_injector_respects_custom_debugger_path(self, run_process, _which):
        run_process.return_value = subprocess.CompletedProcess([], 0, "", "")
        env = Environment(gdb_executable="/opt/custom/gdb")
        injector = NormalExecution(
            env,
            "/tmp/program",
            "/tmp/main.c",
            "target",
            debugger=env.gdb_executable,
        )
        injector.run()
        command = run_process.call_args.args[0]
        self.assertEqual(command[0], "/opt/custom/gdb")

    @patch("src.injector.platform.system", return_value="Darwin")
    @patch(
        "src.injector.shutil.which",
        side_effect=lambda executable: executable if executable == "lldb" else None,
    )
    def test_automatically_selects_installed_debugger(self, _which, _system):
        injector = NormalExecution(Environment(), "/tmp/program", "/tmp/main.c")
        self.assertEqual(injector._debuggers(), ["lldb"])

    def test_builds_lldb_command_with_matching_script(self):
        injector = NormalExecution(
            Environment(), "/tmp/program", "/tmp/main.c", debugger="lldb"
        )
        command = injector._debugger_command(
            "lldb", injector.lldb_script, ["one"]
        )
        self.assertEqual(command[0:3], ["lldb", "--batch", "--no-lldbinit"])
        self.assertIn("extract_baseline_state_lldb.py", command[4])
        self.assertEqual(command[-3:], ["--", injector.target_bin, "one"])



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
                injector.target_bin,
                "one",
                "two",
            ],
        )
        self.assertEqual(call_args.kwargs["env"]["ENTRY_FUNC"], "main")
        self.assertEqual(call_args.kwargs["timeout"], 5)


class InjectorImplementationsTest(unittest.TestCase):
    def test_factory_creates_register_injector(self):
        injectors = BaseInjector.create_for_target(
            RegisterInjection(data_only=True),
            Environment(),
            "program",
            "main.c",
            TargetFunction("work"),
            debugger="gdb",
            timeout_ms=1000,
        )

        self.assertEqual(len(injectors), 1)
        self.assertIsInstance(injectors[0], RegisterInjector)
        self.assertTrue(injectors[0].data_only)
        self.assertEqual(injectors[0].debugger, "gdb")
        self.assertEqual(injectors[0].timeout_ms, 1000)

    def test_factory_creates_memory_injector_for_each_variable(self):
        injectors = BaseInjector.create_for_target(
            MemoryInjection(),
            Environment(),
            "program",
            "main.c",
            TargetFunction(
                "work",
                [TargetVariable("buffer", 8), TargetVariable("value")],
            ),
        )

        self.assertEqual(len(injectors), 2)
        self.assertTrue(all(isinstance(item, MemoryInjector) for item in injectors))
        self.assertEqual(
            [(item.payload_ptr, item.length_var) for item in injectors],
            [("buffer", "8"), ("value", "")],
        )

    def test_factory_creates_generic_memory_injector_without_variables(self):
        injectors = BaseInjector.create_for_target(
            MemoryInjection(),
            Environment(),
            "program",
            "main.c",
            TargetFunction("work"),
        )

        self.assertEqual(len(injectors), 1)
        self.assertIsInstance(injectors[0], MemoryInjector)
        self.assertEqual(injectors[0].payload_ptr, "")

    def test_factory_creates_global_injector(self):
        injectors = BaseInjector.create_for_target(
            GlobalInjection(target_global="counter"),
            Environment(),
            "program",
            "main.c",
            TargetFunction("work"),
        )

        self.assertEqual(len(injectors), 1)
        self.assertIsInstance(injectors[0], GlobalInjector)
        self.assertEqual(injectors[0].target_global, "counter")

    def test_register_injector_setup(self):
        injector = RegisterInjector(Environment(), "program", "main.c", "work", data_only=True)
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
        self.assertTrue(
            injector.lldb_script.endswith("gdb_scripts/inject_registers_lldb.py")
        )

    def test_register_injector_requires_complete_profile(self):
        injector = RegisterInjector(Environment(), "program", "main.c", "work")
        for profile in ({}, {"MAX_STEPS": "1"}, {"TARGET_REGS": "rax"}):
            with self.subTest(profile=profile):
                injector.profile_target = Mock(return_value=profile)
                with self.assertRaisesRegex(RuntimeError, "Profiling failed"):
                    injector.setup()

    def test_memory_injector_setup(self):
        injector = MemoryInjector(
            Environment(),
            "program",
            "main.c",
            "work",
            payload_ptr="buffer",
            length_var="size",
        )
        injector.profile_target = Mock(return_value={"MAX_STEPS": "12"})
        self.assertEqual(
            injector.setup(),
            {
                "TARGET_FUNC": "work",
                "MAX_STEPS": "12",
                "PAYLOAD_PTR": "buffer",
                "LENGTH_VAR": "size",
                "DATA_ONLY_MODE": "1",
            },
        )
        self.assertTrue(injector.gdb_script.endswith("gdb_scripts/inject_memory.py"))
        self.assertTrue(
            injector.lldb_script.endswith("gdb_scripts/inject_memory_lldb.py")
        )

    def test_memory_injector_requires_profiled_steps(self):
        injector = MemoryInjector(Environment(), "program", "main.c", "work")
        injector.profile_target = Mock(return_value={})
        with self.assertRaisesRegex(RuntimeError, "Profiling failed"):
            injector.setup()

    def test_global_injector_setup(self):
        injector = GlobalInjector(
            Environment(),
            "program",
            "main.c",
            "work",
            target_global="my_var",
        )
        injector.profile_target = Mock(return_value={"MAX_STEPS": "42"})
        self.assertEqual(
            injector.setup(),
            {
                "TARGET_FUNC": "work",
                "MAX_STEPS": "42",
                "TARGET_GLOBAL": "my_var",
                "TARGET_SRC": "main.c",
            },
        )
        self.assertTrue(injector.gdb_script.endswith("gdb_scripts/inject_globals.py"))
        self.assertTrue(
            injector.lldb_script.endswith("gdb_scripts/inject_globals_lldb.py")
        )

    def test_global_injector_requires_profiled_steps(self):
        injector = GlobalInjector(Environment(), "program", "main.c", "work")
        injector.profile_target = Mock(return_value={})
        with self.assertRaisesRegex(RuntimeError, "Profiling failed"):
            injector.setup()

    def test_normal_execution_setup(self):
        injector = NormalExecution(Environment(), "program", "main.c", "main")
        self.assertEqual(injector.setup(), {})
        self.assertTrue(
            injector.gdb_script.endswith("gdb_scripts/extract_baseline_state.py")
        )
        self.assertTrue(
            injector.lldb_script.endswith(
                "gdb_scripts/extract_baseline_state_lldb.py"
            )
        )


if __name__ == "__main__":
    unittest.main()
