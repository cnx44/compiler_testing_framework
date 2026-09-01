#include <stdio.h>

int A = 0;
int B = 100;
int C = 200;

void do_work() {
    for (int i = 0; i < 50; i++) {
        A += 1;
    }
    printf("Value of A: %d, B: %d, C: %d (Expected: 50, 100, 200)\\n", A, B, C);
    fflush(stdout);
}

int main() {
    setvbuf(stdout, NULL, _IONBF, 0);
    do_work();
    return 0;
}