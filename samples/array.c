#include <stdio.h>

#define SIZE 10

void foo(int *arr, int l) {
    for(int i=0;i<l;i++) {
        printf("%d ", arr[i]);  
    }
    printf("\n");
}

int main() {
    int l = SIZE;
    int arr[SIZE] = {1,2,3,4,5};

    foo(arr, l);
    return 0;
}