import sys
import os

# Add the project root to sys.path so we can import src
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.injector import RegisterInjector

def main():
    print("Testing Fault Injection on dummy program (samples/exe1.c)...")
    
    # 1. First, compile the dummy program
    print("\n[1] Compiling samples/exe1.c...")
    os.system("gcc -g -O0 samples/exe1.c -o samples/exe1 -lm")
    
    # 2. Instantiate the RegisterInjector for the 'usqrt' function
    print("\n[2] Instantiating RegisterInjector for function 'usqrt'...")
    injector = RegisterInjector(
        target_bin="samples/exe1", 
        target_src="samples/exe1.c", 
        target_func="usqrt"
    )
    
    # 3. Run the injection!
    print("\n[3] Running injection...")
    result = injector.run()
    
    print("\n--- Injection Results ---")
    print(f"Termination Event: {result.event}")
    print(f"Stdout Output: {result.state.stdout}")
    
    print("\nVariables dumped at return line:")
    for var, val in result.state.variables.items():
        print(f"  {var} = {val}")
        
    print("\nCheck out result.state.registers for full register dump!")

if __name__ == "__main__":
    main()
