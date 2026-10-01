#include <stdio.h>
#include <stdlib.h>
#define SIZE 300

static int A[SIZE][SIZE];
static int B[SIZE][SIZE];
static int C[SIZE][SIZE];

void initialize() {
    for (int i = 0; i < SIZE; i++) {
        for (int j = 0; j < SIZE; j++) {
            A[i][j] = i + j;
            B[i][j] = i - j;
            C[i][j] = 0;
        }
    }
}

void compute() {
    for (int i = 0; i < SIZE; i++) {
        for (int j = 0; j < SIZE; j++) {
            int sum = 0;
            for (int k = 0; k < SIZE; k++) {
                if ((i + j + k) % 2 == 0) {
                    sum += A[i][k] * B[k][j];
                } else {
                    sum -= A[i][k] * B[k][j];
                }
            }
            C[i][j] = sum;
        }
    }
}

int main() {
    initialize();
    compute();
    printf("Result check: %d\n", C[SIZE/2][SIZE/2]);
    return 0;
}
