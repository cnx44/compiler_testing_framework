#!/bin/bash

# Check if 1 or 2 arguments are provided
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 <SOURCE_NAME> [--data-only]"
    echo "Example: $0 bubble_sort --data-only"
    exit 1
fi

SOURCE_NAME="$1"
OPT_FLAG="$2" # Captures the flag if provided, otherwise remains empty

BIN_DIR="./fault_injection/${SOURCE_NAME}/compiled/" 
CAMPAIGN_SCRIPT="./fault_injection/run_campaign.sh"
TARGETS_FILE="./fault_injection/${SOURCE_NAME}/targets.cfg"

# Check if the directory exists using -d
if [ ! -d "$BIN_DIR" ]; then
    echo "Error: Directory $BIN_DIR not found. Please compile the files first."
    exit 1
fi

# Check if the campaign script exists using -f
if [ ! -f "$CAMPAIGN_SCRIPT" ]; then
    echo "Error: Campaign script $CAMPAIGN_SCRIPT not found."
    exit 1
fi

# Check if the new targets.cfg exists
if [ ! -f "$TARGETS_FILE" ]; then
    echo "Error: Target configuration file $TARGETS_FILE not found."
    exit 1
fi

echo "All required directories and campaign scripts found. Proceeding with execution..."

# Log file logic: Split based on the data-only flag
if [ "$OPT_FLAG" == "--data-only" ]; then
    LOG_FILE="fault_injection/${SOURCE_NAME}/final_results_data_only.log"
else
    LOG_FILE="fault_injection/${SOURCE_NAME}/final_results_standard.log"
fi

# Clear previous log if it exists
> "$LOG_FILE"

echo "Starting full evaluation suite..." | tee -a "$LOG_FILE"
if [ "$OPT_FLAG" == "--data-only" ]; then
    echo "Mode: DATA-ONLY (Filtered Registers & Variable-Aware Memory)" | tee -a "$LOG_FILE"
else
    echo "Mode: STANDARD (Full Registers & Safe Stack Bounds)" | tee -a "$LOG_FILE"
fi
echo "Results will be concurrently saved to $LOG_FILE" | tee -a "$LOG_FILE"
echo "------------------------------------------------------------" | tee -a "$LOG_FILE"

# Extract expected output using the Python script
EXPECTED_OUTPUT=$(python3 fault_injection/extract_expected.py "$SOURCE_NAME")
EXTRACT_STATUS=$?

# Strict validation
if [ $EXTRACT_STATUS -ne 0 ] || [ -z "$EXPECTED_OUTPUT" ]; then
    echo "Fatal Error: Could not find a valid 'expected_output' for '$SOURCE_NAME' in test.toml."
    echo "Please ensure the benchmark is registered in the TOML file before running fault injection."
    exit 1
fi

export EXPECTED_OUTPUT

# Iterate over every compiled .elf file in the target directory
for BINARY in "$BIN_DIR"/*.elf; do
    # Guard clause in case the directory is empty
    if [ ! -f "$BINARY" ]; then
        echo "Error: No .elf files found in $BIN_DIR"
        exit 1
    fi

    echo "============================================================" | tee -a "$LOG_FILE"
    echo "EVALUATING BINARY: $BINARY" | tee -a "$LOG_FILE"
    echo "============================================================" | tee -a "$LOG_FILE"

    # Read targets.cfg line by line, ignoring empty lines and comments
    grep -v '^#' "$TARGETS_FILE" | grep -v '^$' | while read -r TARGET_FUNC PAYLOAD_PTR LENGTH_VAR DATA_TYPE_SIZE; do
        
        echo ">>> Testing Function: $TARGET_FUNC" | tee -a "$LOG_FILE"
        
        # Export the extracted variables so run_campaign.sh and Python can read them
        export TARGET_FUNC
        export PAYLOAD_PTR
        export LENGTH_VAR
        export DATA_TYPE_SIZE

        # 1. Run the Register Injection Campaign
        echo " -> Starting Register Campaign" | tee -a "$LOG_FILE"
        
        # Pass 'reg' instead of the python path, and pass OPT_FLAG seamlessly
        "$CAMPAIGN_SCRIPT" "$BINARY" reg $OPT_FLAG | while IFS= read -r line; do
            echo "    $line" 
            if [[ ! "$line" == Completed* ]]; then
                echo "$line" >> "$LOG_FILE" 
            fi
        done
        
        echo "------------------------------------------------------------" | tee -a "$LOG_FILE"

        # 2. Run the Memory Injection Campaign
        echo " -> Starting Memory Campaign" | tee -a "$LOG_FILE"
        
        # Pass 'mem' instead of the python path, and pass OPT_FLAG seamlessly
        "$CAMPAIGN_SCRIPT" "$BINARY" mem $OPT_FLAG | while IFS= read -r line; do
            echo "    $line"
            if [[ ! "$line" == Completed* ]]; then
                echo "$line" >> "$LOG_FILE"
            fi
        done
        
        echo "------------------------------------------------------------" | tee -a "$LOG_FILE"
        
    done
done

echo "All campaigns complete! You can view the final metrics in $LOG_FILE"