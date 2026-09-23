import os
import subprocess
import tempfile
from pathlib import Path
import sys

# Add the src folder to Python path (going up one directory from samples/)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.env import Environment
from src.injector import GlobalInjector

c_code = """
#include <stdio.h>

int A = 1;
int B = 100;
int C = 200;

void do_work() {
    // A loop of 50 iterations to give a window for injection without hanging GDB profiling
    for (int i = 0; i < 50; i++) {
        A += 1;
    }
    printf("Value of B: %d\\n", B);
    printf("Value of C: %d\\n", C);
    printf("Value of A: %d (Expected: 51)\\n", A);
    fflush(stdout);
}

int main() {
    setvbuf(stdout, NULL, _IONBF, 0);
    do_work();
    return 0;
}
"""

def compile_c_code(src_file, bin_file):
    # -O0 ensures A is written to memory in the loop, making it a great target!
    subprocess.run(["gcc", "-g", "-O0", src_file, "-o", bin_file], check=True)

def run_tests():
    with tempfile.TemporaryDirectory() as tmpdir:
        src_file = Path(tmpdir) / "test.c"
        bin_file = Path(tmpdir) / "test_bin"
        src_file.write_text(c_code)
        compile_c_code(str(src_file), str(bin_file))
        
        env = Environment()
        env.gdb_executable = "gdb"
        
        print("# Global Variables Fault Injection Tests\\n")
        print("The test program defines three global variables:")
        print("- `int A = 1;` (incremented 50 times in a loop)")
        print("- `int B = 100;`")
        print("- `int C = 200;`\\n")
        
        def test_scenario(target_global, name, num_runs=3):
            print(f"## Scenario: {name}\\n")
            injector = GlobalInjector(
                env=env,
                target_bin=bin_file,
                target_src=src_file,
                target_func="do_work",
                target_global=target_global
            )
            
            for i in range(num_runs):
                print(f"### Run {i+1}")
                print("```text")
                try:
                    result = injector.run()
                    print(f"Event: {result.event}")
                    
                    diag_lines = [line for line in result.state.debugger_output.splitlines() if "[DIAG]" in line or "[*] Injected" in line]
                    if diag_lines:
                        print("Injection Details:")
                        for line in diag_lines:
                            print(f"  {line}")
                            
                    if result.state.stdout:
                        print("Program Output:")
                        # indent program output
                        for out_line in result.state.stdout.strip().splitlines():
                            print(f"  {out_line}")
                    else:
                        print("Program Output: (None, likely crashed or output suppressed)")
                        
                except Exception as e:
                    print(f"Exception during run: {e}")
                print("```\\n")
                    
        test_scenario("A", "Explicit Target (`A`)", 3)
        test_scenario("B", "Explicit Target (`B`)", 3)
        test_scenario("C", "Explicit Target (`C`)", 3)
        test_scenario("", "Random Selection", 5)

if __name__ == "__main__":
    run_tests()
