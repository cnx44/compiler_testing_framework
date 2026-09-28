"""Tests that compile small programs and really run the GDB scripts."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.campaign_runner import OracleRunner
from src.config_parser import GlobalInjection, MemoryInjection, TargetFunction, TargetVariable
from src.env import Environment
from src.injector import create_injectors
from src.models import TerminationEvent

COMPILER = shutil.which("clang") or shutil.which("gcc")


@unittest.skipUnless(COMPILER and shutil.which("gdb"), "requires a C compiler and gdb")
class GdbIntegrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def compile(self, code):
        source = self.directory / "main.c"
        source.write_text(code)
        binary = self.directory / "program"
        subprocess.run([COMPILER, "-g", "-O0", str(source), "-o", str(binary)], check=True)
        return source, binary

    def inject(self, code, injection, function, args=()):
        source, binary = self.compile(code)
        [injector] = create_injectors(injection, "gdb", binary, source, function, list(args))
        return injector.run()

    def test_snapshot_of_one_line_main_keeps_registers(self):
        source = self.directory / "main.c"
        source.write_text("int main(void) { int x = 1; return x - 1; }\n")
        runner = OracleRunner(Environment())
        self.addCleanup(runner.cleanup)
        runner.compile([source])
        snapshot = runner.snapshot([], TargetFunction("main"))
        self.assertIn("rip", snapshot["registers"])

    def test_snapshot_prints_requested_globals_and_arrays(self):
        source = self.directory / "main.c"
        source.write_text("int G = 5;\n"
                          "int sum(int *v, int n) {\n int s = 0;\n"
                          " for (int i = 0; i < n; i++) s += v[i];\n return s;\n}\n"
                          "int main(void) {\n int a[3] = {1, 2, 3};\n return sum(a, 3) - 6;\n}\n")
        runner = OracleRunner(Environment())
        self.addCleanup(runner.cleanup)
        runner.compile([source])
        function = TargetFunction("sum", [TargetVariable("v", "n"), TargetVariable("G", is_global=True)])
        variables = runner.snapshot([], function)["variables"]
        self.assertEqual((variables["v"], variables["G"], variables["s"]), ("{1, 2, 3}", "5", "6"))

    def test_memory_injection_infers_static_array_length(self):
        result = self.inject(
            "void work() {\n int arr[12];\n for (int i = 0; i < 12; i++) arr[i] = i;\n}\n"
            "int main() {\n work();\n return 0;\n}\n",
            MemoryInjection(), TargetFunction("work", [TargetVariable("arr")]))
        self.assertIn("Inferred static array length: 12 elements", result.state.debugger_output)
        self.assertIn("Total payload size: 48 bytes", result.state.debugger_output)
        self.assertIn("[*] Injected memory", result.state.debugger_output)

    def test_memory_injection_of_a_scalar(self):
        result = self.inject(
            "void work() {\n double my_var = 3.14;\n my_var *= 2;\n}\n"
            "int main() {\n work();\n return 0;\n}\n",
            MemoryInjection(), TargetFunction("work", [TargetVariable("my_var")]))
        self.assertIn("Defaulting to 1 element.", result.state.debugger_output)
        self.assertIn("Total payload size: 8 bytes", result.state.debugger_output)

    def test_injection_keeps_program_arguments_and_output(self):
        result = self.inject(
            '#include <stdio.h>\nint unused = 0;\n'
            'int main(int argc, char **argv) {\n int n = argc;\n n++;\n unused++;\n'
            ' printf("%s %d\\n", argv[1], argc);\n return 0;\n}\n',
            GlobalInjection(), TargetFunction("main", [TargetVariable("unused", is_global=True)]),
            args=["hello"])
        self.assertEqual(result.event, TerminationEvent.RETURNED)
        self.assertEqual(result.state.stdout, "hello 2")
        self.assertEqual(result.state.variables["argc"], "2")


if __name__ == "__main__":
    unittest.main()
