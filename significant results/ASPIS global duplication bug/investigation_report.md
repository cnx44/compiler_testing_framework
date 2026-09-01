# Investigation Report: Global Variable Injection Discrepancy

## Summary of Findings

The discrepancy in injection outcomes is not due to a difference in how EDDI and RASM protect global variables. In fact, **both protection schemes fail to protect global variables due to a shared bug in the ASPIS LLVM pass**. 

The varying outcomes across different test runs are caused by a combination of this LLVM pass bug and the exact instruction-level timing of when the GDB script injects the fault.

## 1. The ASPIS LLVM Pass Bug (Why Protection Fails)

When ASPIS applies data protection (EDDI/RASM), it duplicates the global variables (creating `A_dup`) and clones the instructions that operate on them. 

However, **the pass fails to update the pointer operands of the cloned instructions**.

Instead of the cloned load/store pointing to the duplicated variable (`A_dup`), it continues to point to the original variable (`A`). 

```assembly
    # Disassembly of do_work loop in test_eddi.elf
    11e8:       8b 05 52 2e 00 00       mov    0x2e52(%rip),%eax        # 4040 <A>
    11ee:       8b 0d 4c 2e 00 00       mov    0x2e4c(%rip),%ecx        # 4040 <A>
```

Because both the original and shadow registers (`eax` and `ecx`) load from and store to the exact same memory address (`A`), the duplicated variables are **never used**. The validation check at the end of the operation effectively compares the variable against itself (`if (A != A)`), which always passes.

## 2. Why injecting into `do_work()` yields 61% SDC, 29% Masked, and 2% ASPIS_DATA

In your recent run, you configured the framework to inject faults directly into `do_work()`. This forces the GDB script to inject faults randomly across the instructions of the `do_work()` loop. 

Because `A` is updated in this loop, the outcome is entirely dictated by **exactly which instruction the fault is injected at**. The ~40 instructions in the loop body can be divided into three distinct "windows", which perfectly predict your results:

### Window 1: ASPIS_DATA Detected (1 instruction, ~2.5% probability)
If the fault is injected **exactly between the two load instructions** (after `mov 4040, %eax` but before `mov 4040, %ecx`), `%eax` loads the original uncorrupted value, while `%ecx` loads the newly corrupted value. Because the registers now hold different values, the EDDI check `cmp %ecx, %eax` finally fails, and `ASPIS_DATA` is detected. Since this window is only 1 instruction long out of the ~40 in the loop body, it occurs rarely (observed: 2%).

### Window 2: Masked Fault (~12 instructions, ~30% probability)
If the fault is injected **after the second load, but before the store**, both `%eax` and `%ecx` have already safely loaded the correct value. The memory at `A` is corrupted, but the store instruction at the end of the loop overwrites the corrupted memory with the correct value from the registers. The fault is erased, resulting in a **Masked** outcome (observed: 29%).

### Window 3: Silent Data Corruption (~27 instructions, ~67% probability)
If the fault is injected **anywhere else** (e.g., before the first load, or after the store), the memory at `A` is corrupted and is not overwritten during that iteration. On the *next* iteration, both `%eax` and `%ecx` load the corrupted value. Because both registers load the *same* corrupted value, the broken EDDI check passes (`corrupted == corrupted`), and the program proceeds to print the wrong value, resulting in **SDC** (observed: 61%).

## 3. Why earlier runs had 100% SDC (EDDI) and 50/50 splits (RASM)

In your earlier runs, the injection config did not specify a target function, so the GDB script defaulted to injecting into `main()`.

Because the GDB `nexti` command steps *over* function calls, the script **never injected faults inside the `do_work()` loop**. It only injected faults *before* `do_work()` was called, or *after* it returned.

- **In EDDI**, `main` has no instructions after `do_work()` returns. Thus, 100% of faults were injected before the loop started. The loop loaded the corrupted `A`, bypassed the broken check, and resulted in **100% SDC**.
- **In RASM**, the Control Flow Checking (CFCSS) pass inserts a large block of verification instructions into `main` *after* `do_work()` returns. About 50% of the time, the script injected a fault into these CFCSS instructions (after `do_work()` had already printed the correct values). Corrupting `A` at this late stage had no effect on the output, resulting in the **50% Masked** outcome you observed.

## Conclusion

The "weird" 2 `APSIS_DATA` results perfectly confirm the LLVM pass bug. The protection only works if the fault manages to thread the needle and strike exactly in the 1-instruction window between the duplicate loads! To fix this permanently, the ASPIS LLVM pass must be updated to correctly target the `_dup` shadow variables for all cloned memory operations.
