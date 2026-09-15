import tempfile
import unittest
from pathlib import Path

from src.source_symbols import analyze_source, SourceAnalysisError


class SourceSymbolsTest(unittest.TestCase):
    def analyze(self, suffix, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("source" + suffix)
            path.write_text(source)
            return analyze_source(path)

    def test_comments_strings_and_locals_are_not_globals(self):
        result = self.analyze(".c", '''/* int fake() { } */
int G;
int main(int argc, char **argv) {
 const char *s = "int bogus() {}"; int x = 2;
 return x;
}''')
        self.assertEqual(result.globals, ("G",))
        self.assertEqual([f.name for f in result.functions], ["main"])
        self.assertEqual(result.functions[0].variables, ("argc", "argv", "s", "x"))

    def test_cpp_overloads_are_not_silently_merged(self):
        result = self.analyze(".cpp", "int work(int x) { return x; } double work(double x) { return x; } int main() { int n=0; return n; }")
        self.assertEqual([f.name for f in result.functions], ["main"])
        self.assertTrue(result.warnings)

    def test_rust_scope_and_static(self):
        result = self.analyze(".rs", "static G: i32 = 1; fn work(x: i32) { let y = x; } fn main() { let n = 3; }")
        self.assertEqual(result.globals, ("G",))
        self.assertEqual(result.functions[0].variables, ("x", "y"))
        self.assertEqual(result.functions[1].variables, ("n",))

    def test_invalid_syntax_is_reported(self):
        with self.assertRaises(SourceAnalysisError):
            self.analyze(".c", "int main( { broken")
