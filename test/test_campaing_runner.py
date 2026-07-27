import shutil
import unittest
from pathlib import Path

from src.campaing_runner import Oracle


class OracleTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("clang"), "clang is required")
    @unittest.skipUnless(shutil.which("gdb"), "gdb is required")
    def test_oracle_builder_compiles_runs_and_reads_debug_state(self):
        oracle = Oracle()
        exe_path = Path("samples/exe1")

        results = oracle.oracle_builder("samples/exe1.c", ["0", "0"])

        self.assertEqual(results["stdout"], "")
        self.assertEqual(results["stderr"], "")
        self.assertEqual(oracle.size, 0)
        self.assertFalse(exe_path.exists())
        self.assertIn("variables", results)
        self.assertIn("registers", results)

        self.assertEqual(results["variables"]["argc"], "3")
        self.assertIn("args", results["variables"])
        self.assertEqual(results["variables"]["a"], "0")
        self.assertEqual(results["variables"]["b"], "0")
        self.assertEqual(results["variables"]["c"], "0")
        self.assertGreater(len(results["registers"]), 0)


if __name__ == "__main__":
    unittest.main()
