import unittest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, Mock

from src.injector import MemoryInjector
from src.models import ExecutionState

class TestDynamicInference(unittest.TestCase):
    def test_dynamic_inference_static_array(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            src_file = Path(temp_dir) / "main.c"
            src_file.write_text("""
void work() {
    int arr[12];
    for(int i=0; i<12; i++) arr[i] = i;
}
int main() {
    work();
    return 0;
}
""")
            exe_file = Path(temp_dir) / "prog"
            # Compile with debug symbols!
            subprocess.run(["gcc", "-g", str(src_file), "-o", str(exe_file)], check=True)
            
            injector = MemoryInjector(
                target_bin=str(exe_file), 
                target_src=str(src_file), 
                target_func="work",
                payload_ptr="arr"
            )
            
            # Mock profile_target to avoid running the profiling script
            injector.profile_target = Mock(return_value={"MAX_STEPS": "2"})
            
            # We want to capture the GDB stdout to verify the diagnostic messages
            captured_stdout = ""
            original_extract_state = injector._extract_state
            
            def mock_extract(stdout, stderr, inferior_out):
                nonlocal captured_stdout
                captured_stdout = stdout
                return original_extract_state(stdout, stderr, inferior_out)
                
            injector._extract_state = mock_extract
            
            # Run the injector
            injector.run()
            
            # Assertions
            self.assertIn("[DIAG] Inferred static array length: 12 elements", captured_stdout)
            self.assertIn("Total payload size: 48 bytes", captured_stdout)

    def test_dynamic_inference_single_variable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            src_file = Path(temp_dir) / "main.c"
            src_file.write_text("""
void work() {
    double my_var = 3.14;
}
int main() {
    work();
    return 0;
}
""")
            exe_file = Path(temp_dir) / "prog"
            subprocess.run(["gcc", "-g", str(src_file), "-o", str(exe_file)], check=True)
            
            injector = MemoryInjector(
                target_bin=str(exe_file), 
                target_src=str(src_file), 
                target_func="work",
                payload_ptr="my_var"
            )
            
            injector.profile_target = Mock(return_value={"MAX_STEPS": "2"})
            
            captured_stdout = ""
            original_extract_state = injector._extract_state
            
            def mock_extract(stdout, stderr, inferior_out):
                nonlocal captured_stdout
                captured_stdout = stdout
                return original_extract_state(stdout, stderr, inferior_out)
                
            injector._extract_state = mock_extract
            
            injector.run()
            
            self.assertIn("Defaulting to 1 element.", captured_stdout)
            self.assertIn("Total payload size: 8 bytes", captured_stdout)

if __name__ == "__main__":
    unittest.main()
