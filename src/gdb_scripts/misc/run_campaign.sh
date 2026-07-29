#!/bin/bash

# 1. Check if 2 or 3 arguments are provided
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage: $0 <TARGET_BIN> <reg|mem> [--data-only]"
    echo "Example: $0 ./fault_injection/bubble_sort/compiled/baseline_app.elf reg --data-only"
    exit 1
fi

# 2. Assign arguments to variables
TARGET_BIN="$1"
INJECT_FLAG="$2"
NUM_RUNS=20

if [[ "$INJECT_FLAG" != "reg" && "$INJECT_FLAG" != "mem" ]]; then
    echo "Error: Second argument must be 'reg' or 'mem'."
    exit 1
fi

# 3. Check for exported variables
if [ -z "$TARGET_FUNC" ]; then
    echo "Error: TARGET_FUNC environment variable is not set."
    exit 1
fi

if [ -z "$EXPECTED_OUTPUT" ]; then
    echo "Error: EXPECTED_OUTPUT environment variable is empty. Halting to prevent false positives."
    exit 1
fi

# 4. Locate directories
COMPILED_DIR=$(dirname "$TARGET_BIN")
BENCHMARK_DIR=$(dirname "$COMPILED_DIR")
BASE_DIR=$(dirname "$0")

PROFILE_SCRIPT="$BASE_DIR/profile_target.py"

# 5. Resolve Python Scripts and Modes strictly to the global folder
if [ "$INJECT_FLAG" == "reg" ]; then
    INJECTION_TARGET="REGISTERS"
    GDB_SCRIPT="$BASE_DIR/inject_registers.py"
    
    if [ "$3" == "--data-only" ]; then
        export DATA_ONLY_MODE=1
        MODE_TEXT="DATA-ONLY (Pointers Excluded)"
    else
        export DATA_ONLY_MODE=0
        MODE_TEXT="FULL REGISTER SET"
    fi
else
    INJECTION_TARGET="MEMORY"
    GDB_SCRIPT="$BASE_DIR/inject_memory.py"
    
    if [ "$3" == "--data-only" ]; then
        export DATA_ONLY_MODE=1
        MODE_TEXT="DATA-ONLY (Config-Defined Bounds)"
    else
        export DATA_ONLY_MODE=0
        MODE_TEXT="STANDARD (Safe Stack Frame Bounds)"
    fi
fi

# 6. Verify the resolved scripts exist
if [ ! -f "$TARGET_BIN" ]; then
    echo "Error: Target binary '$TARGET_BIN' not found!"
    exit 1
fi

if [ ! -f "$GDB_SCRIPT" ]; then
    echo "Error: GDB script '$GDB_SCRIPT' not found!"
    exit 1
fi

if [ ! -f "$PROFILE_SCRIPT" ]; then
    echo "Error: Profiler script '$PROFILE_SCRIPT' not found!"
    exit 1
fi

count_masked=0
count_sdc=0
count_crash=0
count_aspis_data=0
count_aspis_cf=0

echo "=== Phase 1: Profiling Target ==="
echo "Target Binary:   $TARGET_BIN"
echo "Target Function: $TARGET_FUNC"

# Run profiling
PROFILE_OUT=$(gdb -batch -q -x "$PROFILE_SCRIPT" --args "$TARGET_BIN" 2>/dev/null)
MAX_STEPS=$(echo "$PROFILE_OUT" | grep "PROFILED_STEPS=" | cut -d'=' -f2)

if [[ -z "$MAX_STEPS" || "$MAX_STEPS" == "ERROR"* ]]; then
    echo "Fatal Error: Could not profile the target. Output: $PROFILE_OUT"
    exit 1
fi

echo "Dynamic instructions inside '$TARGET_FUNC': $MAX_STEPS"
export MAX_STEPS

if [ "$INJECTION_TARGET" == "REGISTERS" ]; then
    PROFILED_REGS=$(echo "$PROFILE_OUT" | grep "PROFILED_REGS=" | cut -d'=' -f2)
    export TARGET_REGS="$PROFILED_REGS"
    echo "Target Registers: $TARGET_REGS"
fi
echo ""

echo "=== Phase 2: Fault Injection Campaign ==="
echo "Using Injector: $GDB_SCRIPT"
echo "Injection Mode: $MODE_TEXT"

for ((i=1; i<=NUM_RUNS; i++)); do
    # Remove the temporary file from the previous run to avoid reading stale data
    rm -f .inferior_out.txt

    output=$(timeout 2s gdb -batch -return-child-result -q -x "$GDB_SCRIPT" --args "$TARGET_BIN" 2>&1)
    exit_code=$?

    # Evaluate execution results
    if [ $exit_code -eq 124 ]; then
        ((count_crash++))
    elif echo "$output" | grep -q "Data corruption detected"; then
        ((count_aspis_data++))
    elif echo "$output" | grep -q "Control-flow signature mismatch"; then
        ((count_aspis_cf++))
    else
        # Check if GDB caught a fatal crash (Segmentation fault, Illegal instruction, etc.)
        if echo "$output" | grep -q -E "Program received signal|Program terminated with signal"; then
            ((count_crash++))
        else
            # Read the clean output from the target program
            ACTUAL_OUTPUT=$(cat .inferior_out.txt 2>/dev/null | xargs)
            
            if [ "$ACTUAL_OUTPUT" == "$EXPECTED_OUTPUT" ]; then
                ((count_masked++)) # Output matches exactly
            elif [ -n "$ACTUAL_OUTPUT" ]; then
                ((count_sdc++))    # Output was generated but it is corrupted
            else
                ((count_crash++))  # Program died silently without outputting anything
            fi
        fi
    fi

    if (( i % (NUM_RUNS / 4) == 0 )); then
        echo "Completed $i / $NUM_RUNS"
    fi
done

echo ""
echo "=== Campaign Results ==="
echo "Target:       $TARGET_BIN"
echo "Target Type:  $INJECTION_TARGET"
echo "Mode:         $MODE_TEXT"
echo "Masked:       $count_masked"
echo "SDC (KO):     $count_sdc"
echo "Crashes:      $count_crash"
echo "ASPIS Data:   $count_aspis_data"
echo "ASPIS CF:     $count_aspis_cf"
echo "========================"