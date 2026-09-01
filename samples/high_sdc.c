#include <stdio.h>
#include <stdint.h>

/*
 * Questo test è progettato per massimizzare la probabilità di SDC (Silent Data Corruption)
 * riducendo al minimo i fault mascherati (Masked).
 * 
 * Strategie utilizzate:
 * 1. Tipi di dato a 64-bit (uint64_t e double): occupano interamente i registri interi (es. rax) 
 *    e usano 64 dei 128 bit dei registri xmm, dimezzando lo spazio "morto" rispetto ai float a 32-bit.
 * 2. Loop intensivo: le variabili vivono nei registri per migliaia di cicli. Il 99% delle
 *    istruzioni dinamiche del programma riguarda calcoli matematici, aumentando la finestra 
 *    di vulnerabilità.
 * 3. Effetto valanga (Avalanche effect): utilizziamo un generatore Xorshift per l'intero. 
 *    Qualsiasi singolo bit flippato in qualsiasi momento cambierà completamente e irrimediabilmente 
 *    tutto il calcolo successivo, garantendo un SDC invece di un mascheramento.
 */
int main() {
    uint64_t int_acc = 0x0123456789ABCDEFULL;
    double float_acc = 1.0;
    
    // Esegue 20 iterazioni per far sì che il programma spenda quasi tutto il suo
    // tempo di esecuzione alterando dati all'interno dei registri (abbastanza per SDC, ma 
    // abbastanza breve da non far andare in timeout GDB durante lo step-by-step).
    for (uint8_t i = 0; i < 20; i++) {
        // Xorshift64: un singolo bit alterato corromperà tutto il valore in modo permanente
        int_acc ^= (int_acc << 13);
        int_acc ^= (int_acc >> 7);
        int_acc ^= (int_acc << 17);
        
        // Calcolo floating point cumulativo
        float_acc = (float_acc * 1.00001) + 0.00002;
    }
    
    printf("%lu %.6f\n", int_acc, float_acc);
    return 0;
}
