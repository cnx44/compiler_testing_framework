#include <stdio.h>
#include <stdlib.h>

__attribute__((weak)) void DataCorruption_Handler(void) {
    printf("Data corruption detected\n");
    exit(1);
}

__attribute__((weak)) void SigMismatch_Handler(void) {
    printf("Control-flow signature mismatch\n");
    exit(1);
}
