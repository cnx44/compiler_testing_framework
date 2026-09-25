#include <stdio.h>

int main() {
    long sum_squares = 0;
    double product = 1.0;
    
    for (int i = 1; i <= 20; i++) {
        sum_squares += i * i;
        product *= 1.5;
    }
    
    printf("%ld %.2f\n", sum_squares, product);
    return 0;
}
