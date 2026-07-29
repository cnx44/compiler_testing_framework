#!/bin/bash

# 1. Check if exactly one argument is provided
if [ "$#" -ne  1 ]; then
    echo "Usage: $0 <SOURCE_NAME>"
    echo "Example: $0 test_1"
    exit 1
fi

SOURCE_NAME="$1"
#SRC_FILE="examples/c-benchmarks/test_1.c"
SRC_FILE="testing/tests/c/test_benchmarks/${SOURCE_NAME}.c"
OUT_DIR="fault_injection/${SOURCE_NAME}/compiled"


if [ ! -f "$SRC_FILE" ]; then
    echo "Error: Source file $SRC_FILE not found."
    exit 1
fi

# Set the path to your LLVM binaries
LLVM_BIN="/usr/lib/llvm-21/bin/" 

# Create output directory if it doesn't exist
mkdir -p "$OUT_DIR"

echo "=== Compiling Baseline ==="
# Fixed: Using the absolute path to clang
"${LLVM_BIN}clang" -g "$SRC_FILE" -o "$OUT_DIR/baseline_app.elf"
echo "-> $OUT_DIR/baseline_app.elf"

echo ""
echo "=== Compiling ASPIS Hardened Versions ==="

# 1. EDDI + CFCSS (High protection, standard baseline from literature)
echo "Compiling EDDI + CFCSS..."
./aspis.sh --llvm-bin "$LLVM_BIN" --eddi --cfcss -g "$SRC_FILE" -o "$OUT_DIR/hardened_eddi_cfcss.elf"

# 2. sEDDI + RASM (Optimized data protection + double-update Control-Flow Checking)
echo "Compiling sEDDI + RASM..."
./aspis.sh --llvm-bin "$LLVM_BIN" --seddi --rasm -g "$SRC_FILE" -o "$OUT_DIR/hardened_seddi_rasm.elf"

# 3. FDSC + CFCSS (Swapped to a more stable configuration to avoid compiler plugin crash)
echo "Compiling FDSC + CFCSS..."
./aspis.sh --llvm-bin "$LLVM_BIN" --fdsc --cfcss -g "$SRC_FILE" -o "$OUT_DIR/hardened_fdsc_cfcss.elf"

echo ""
echo "=== Compilation Complete ==="
ls -lh "$OUT_DIR"/*.elf