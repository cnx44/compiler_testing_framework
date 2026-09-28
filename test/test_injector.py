import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config_parser import (
    GlobalInjection,
    MemoryInjection,
    RegisterInjection,
    TargetFunction,
    TargetVariable,
)
from src.injector import create_injectors, termination_event
from src.models import TerminationEvent

PROFILE = "PROFILED_STEPS=12\nPROFILED_REGS=rax,rbx\n"
DUMP = "@@ARGS@@\n@@LOCALS@@\nx = 4\n@@REGISTERS@@\nrax            0x1    1\n"


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def sequence(*steps):
    """side_effect for subprocess.run: return or call each step in turn."""
    steps = iter(steps)

    def run(command, env, **kwargs):
        step = next(steps)
        if isinstance(step, Exception):
            raise step
        return step(command, env, **kwargs) if callable(step) else step
    return run


def make(injection=RegisterInjection(), function=TargetFunction("work"), timeout_ms=1250):
    return create_injectors(injection, "gdb", "/tmp/program", "/src/main.c", function,
                            ["a"], timeout_ms)


class FactoryTest(unittest.TestCase):
    def test_register_injector(self):
        [injector] = make(RegisterInjection(data_only=True))
        self.assertEqual(injector.script.name, "inject_registers.py")
        self.assertEqual(injector.options, {"DATA_ONLY_MODE": "1"})
        self.assertEqual(injector.target_bin, Path("/tmp/program"))
        self.assertEqual((injector.args, injector.timeout_ms), (["a"], 1250))

    def test_memory_injector_for_each_local_variable(self):
        function = TargetFunction("work", [TargetVariable("buf", "n"), TargetVariable("x"),
                                           TargetVariable("G", is_global=True)])
        injectors = make(MemoryInjection(), function)
        self.assertEqual([i.options for i in injectors], [
            {"PAYLOAD_PTR": "buf", "LENGTH_VAR": "n"},
            {"PAYLOAD_PTR": "x", "LENGTH_VAR": ""},
        ])
        [untargeted] = make(MemoryInjection())
        self.assertEqual((untargeted.script.name, untargeted.options), ("inject_memory.py", {}))

    def test_global_injector_for_each_global_variable(self):
        function = TargetFunction("work", [TargetVariable("x"), TargetVariable("G", is_global=True)])
        [targeted] = make(GlobalInjection(), function)
        self.assertEqual((targeted.script.name, targeted.options), ("inject_globals.py", {"TARGET_GLOBAL": "G"}))
        [untargeted] = make(GlobalInjection())
        self.assertEqual(untargeted.options, {"TARGET_SRC": "main.c"})


@patch("src.injector.subprocess.run")
class InjectorTest(unittest.TestCase):
    def setUp(self):
        function = TargetFunction("work", [TargetVariable("x", 4)])
        [self.injector] = make(RegisterInjection(), function)

    def test_profile_runs_once_and_is_passed_to_every_injection(self, run):
        run.side_effect = [completed(stdout=PROFILE), completed(stdout=DUMP), completed(stdout=DUMP)]
        self.injector.run()
        self.injector.run()

        profile_command, injection_command = run.call_args_list[0], run.call_args_list[1]
        self.assertTrue(profile_command.args[0][5].endswith("profile_target.py"))
        self.assertEqual(profile_command.args[0][6:], ["--args", "/tmp/program", "a"])
        self.assertEqual(profile_command.kwargs["timeout"], 1.25)
        self.assertEqual(run.call_count, 3)
        env = injection_command.kwargs["env"]
        self.assertTrue(injection_command.args[0][5].endswith("inject_registers.py"))
        self.assertEqual({k: env[k] for k in ("TARGET_FUNC", "MAX_STEPS", "TARGET_REGS",
                                              "DATA_ONLY_MODE", "DUMP_VARS")},
                         {"TARGET_FUNC": "work", "MAX_STEPS": "12", "TARGET_REGS": "rax,rbx",
                          "DATA_ONLY_MODE": "0", "DUMP_VARS": "x:4"})
        self.assertNotEqual(env["INFERIOR_OUT"], env["INFERIOR_ERR"])

    def test_profile_failures(self, run):
        for output, message in (("PROFILED_STEPS=ERROR", "profiling failed"),
                                ("PROFILED_STEPS=0\nPROFILED_REGS=rax", "no instruction"),
                                ("PROFILED_STEPS=3\nPROFILED_REGS=", "No register")):
            with self.subTest(output=output):
                run.side_effect = [completed(stdout=output)]
                self.injector._profile = None
                with self.assertRaisesRegex(RuntimeError, message):
                    self.injector.run()

    def test_profile_timeout(self, run):
        run.side_effect = subprocess.TimeoutExpired("gdb", 1.25)
        with self.assertRaisesRegex(RuntimeError, "timed out after 1250 ms"):
            self.injector.run()

    def test_run_reads_state_and_redirected_program_output(self, run):
        def inject(command, env, **kwargs):
            Path(env["INFERIOR_OUT"]).write_text("program out\n")
            Path(env["INFERIOR_ERR"]).write_text("program err\n")
            return completed(stdout="[*] Injected\n" + DUMP, stderr="gdb warning")

        run.side_effect = sequence(completed(stdout=PROFILE), inject)
        result = self.injector.run()
        self.assertEqual(result.event, TerminationEvent.RETURNED)
        self.assertEqual((result.state.stdout, result.state.stderr), ("program out", "program err"))
        self.assertEqual(result.state.variables, {"x": "4"})
        self.assertEqual(result.state.registers, {"rax": "0x1"})
        self.assertIn("gdb warning", result.state.debugger_output)
        self.assertEqual(result.state.error, "")

    def test_detection_printed_by_the_program(self, run):
        def inject(command, env, **kwargs):
            Path(env["INFERIOR_OUT"]).write_text("DATA_CORRUPTION_DETECTED\n")
            return completed(stdout="[Inferior 1 (process 1) exited with code 01]\n")

        run.side_effect = sequence(completed(stdout=PROFILE), inject)
        result = self.injector.run()
        self.assertEqual(result.event, TerminationEvent.ASPIS_DATA)
        self.assertEqual(result.state.error, "The target function did not reach its return.")

    def test_timeout_keeps_partial_output(self, run):
        run.side_effect = [completed(stdout=PROFILE),
                           subprocess.TimeoutExpired("gdb", 1.25, output=b"partial", stderr=None)]
        result = self.injector.run()
        self.assertEqual(result.event, TerminationEvent.TIMEOUT)
        self.assertEqual(result.state.debugger_output, "partial")


class TerminationEventTest(unittest.TestCase):
    def test_events(self):
        cases = [
            (0, "Data corruption detected", TerminationEvent.ASPIS_DATA),
            (0, "SIG_MISMATCH_DETECTED", TerminationEvent.ASPIS_CF),
            (0, "Program received signal SIGSEGV", TerminationEvent.CRASHED),
            (0, "Program terminated with signal SIGKILL", TerminationEvent.CRASHED),
            (0, "[FATAL] Uncaught exception: x", TerminationEvent.INTERNAL_ERROR),
            (1, "", TerminationEvent.INTERNAL_ERROR),
            (0, "", TerminationEvent.RETURNED),
        ]
        for exit_code, output, event in cases:
            with self.subTest(output=output):
                self.assertIs(termination_event(exit_code, output), event)


if __name__ == "__main__":
    unittest.main()
