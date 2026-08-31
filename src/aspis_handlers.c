#include <stdio.h>
#include <stdlib.h>

void DataCorruption_Handler(void) {
    printf("Data corruption detected\n");
    exit(1);
}

void SigMismatch_Handler(void) {
    printf("Control-flow signature mismatch\n");
    exit(1);
}
