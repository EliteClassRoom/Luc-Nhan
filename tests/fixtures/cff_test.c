/*
 * CFF test binary for Luc Nhan IL analysis tools.
 * Compile with: clang -O0 -o cff_test cff_test.c
 */

#include <stdio.h>
#include <stdlib.h>

/* Opaque predicate: always returns 1, but uses volatile to prevent optimization */
int always_true(void) {
    volatile int x = 1;
    return x;
}

/* Opaque predicate: always returns 0, but uses volatile to prevent optimization */
int always_false(void) {
    volatile int x = 0;
    return x;
}

/* Should never be called -- marks dead code that was actually reached */
void dead_code_trap(void) {
    fprintf(stderr, "DEAD CODE REACHED\n");
    abort();
}

/*
 * Control-flow-flattened buffer processor.
 *
 * State machine with 8 switch cases:
 *   6 real processing states, 1 dead code state, 1 exit state.
 *
 * State transition order:
 *   0x10 -> 0x25 -> 0x37 -> 0x4A -> 0x5C -> 0x6E -> 0xFF (exit)
 *
 * Dead code state 0xDE is never reached from any valid transition.
 * Two opaque predicates create additional dead-code paths.
 */
void process_buffer_cff(unsigned char *buf, int len) {
    int state = 0x10;
    int i = 0;
    unsigned int checksum = 0;

    while (state != 0xFF) {
        switch (state) {

        /* State 0x10: Initialize -- XOR first byte into checksum */
        case 0x10:
            if (len > 0) {
                checksum ^= buf[0];
            }
            /* Opaque predicate 1: always_true() is always true */
            if (always_true()) {
                state = 0x25;
            } else {
                /* Dead path -- never taken */
                dead_code_trap();
                state = 0xDE;
            }
            break;

        /* State 0x25: Accumulate -- add each byte to checksum */
        case 0x25:
            for (i = 0; i < len; i++) {
                checksum += buf[i];
            }
            state = 0x37;
            break;

        /* State 0x37: Transform -- rotate checksum and XOR with length */
        case 0x37:
            checksum = (checksum << 3) | (checksum >> 29);
            checksum ^= (unsigned int)len;
            state = 0x4A;
            break;

        /* State 0x4A: Validate -- bounds-check the buffer length */
        case 0x4A:
            if (len < 0) {
                /* Invalid length, skip to exit */
                state = 0xFF;
            } else {
                state = 0x5C;
            }
            break;

        /* State 0x5C: Finalize -- fold checksum to 16 bits */
        case 0x5C:
            checksum = (checksum & 0xFFFF) ^ (checksum >> 16);
            /* Opaque predicate 2: always_false() is always false */
            if (always_false()) {
                /* Dead path -- never taken */
                dead_code_trap();
                state = 0xDE;
            } else {
                state = 0x6E;
            }
            break;

        /* State 0x6E: Report -- print the result and proceed to exit */
        case 0x6E:
            printf("process_buffer_cff: checksum = 0x%04X (len=%d)\n",
                   checksum & 0xFFFF, len);
            state = 0xFF;
            break;

        /* State 0xDE: Dead code -- never reached from valid transitions */
        case 0xDE:
            dead_code_trap();
            state = 0xFF;
            break;

        default:
            /* Unknown state -- abort */
            fprintf(stderr, "process_buffer_cff: unexpected state 0x%X\n", state);
            state = 0xFF;
            break;
        }
    }
}

/*
 * Simple 4-case CFF for smoke testing.
 *
 * State transition order: 1 -> 2 -> 3 -> 0 (exit)
 * Performs simple arithmetic and prints the result.
 */
int simple_cff(int input) {
    int state = 1;
    int result = input;

    while (state != 0) {
        switch (state) {

        /* State 1: multiply by 3 */
        case 1:
            result = result * 3;
            state = 2;
            break;

        /* State 2: add 7 */
        case 2:
            result = result + 7;
            state = 3;
            break;

        /* State 3: XOR with 0xAA */
        case 3:
            result = result ^ 0xAA;
            state = 0;
            break;

        default:
            state = 0;
            break;
        }
    }

    return result;
}

int main(int argc, char *argv[]) {
    /* Exercise simple_cff */
    int val = simple_cff(42);
    printf("simple_cff(42) = %d\n", val);

    /* Exercise process_buffer_cff */
    unsigned char sample[] = { 0x48, 0x65, 0x6C, 0x6C, 0x6F };
    process_buffer_cff(sample, sizeof(sample));

    /* Edge case: empty buffer */
    process_buffer_cff(NULL, 0);

    return 0;
}
