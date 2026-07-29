import sys
import tomllib

# Ensure a target benchmark name is provided
if len(sys.argv) != 2:
    sys.exit(1)

target_name = sys.argv[1]

try:
    # Adjust path if your toml is located elsewhere
    with open("testing/config/tests.toml", "rb") as f:
        config_data = tomllib.load(f)
    
    for test in config_data.get("tests", []):
        # Match the test_name to the target
        if test.get("test_name") == target_name:
            expected = test.get("expected_output")
            if expected is not None:
                print(expected)
                sys.exit(0)
    
    # Target found, but no expected_output defined
    sys.exit(2)
except Exception as e:
    # File missing or TOML syntax error
    sys.exit(3)