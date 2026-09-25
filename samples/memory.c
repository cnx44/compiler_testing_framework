#include <stdio.h>

#define SIZE 20

int main() {
    int arr[SIZE];
    int sum = 0;

    for (int i = 0; i < SIZE; i++) {
        arr[i] = i * 10;
    }

    for (int i = 0; i < SIZE; i++) {
        sum += arr[i];
    }

    printf("Sum: %d\n", sum);
    return 0;
}
