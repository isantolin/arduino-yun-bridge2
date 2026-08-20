/*
 * simavr_harness.c - Dedicated AVR Hardware Simulation Harness with UART PTY Bridge
 *
 * Links simulated AVR microcontroller (ATmega32u4 / ATmega2560 / ATmega328P) UART
 * to a Linux Pseudo-Terminal (PTY) for full hardware emulation with mcubridge daemon.
 */

#include <libgen.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <simavr/avr_uart.h>
#include <simavr/sim_avr.h>
#include <simavr/sim_elf.h>
#include <simavr/uart_pty.h>

static volatile int running = 1;

static void sig_handler(int sig) {
    (void)sig;
    running = 0;
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <firmware.elf> [mcu] [frequency_hz]\n", argv[0]);
        return 1;
    }

    const char *firmware_file = argv[1];
    const char *mcu_name = (argc > 2) ? argv[2] : "atmega2560";
    uint32_t frequency = (argc > 3) ? (uint32_t)strtoul(argv[3], NULL, 10) : 16000000UL;

    elf_firmware_t firmware;
    memset(&firmware, 0, sizeof(firmware));

    if (elf_read_firmware(firmware_file, &firmware) != 0) {
        fprintf(stderr, "[ERROR] Failed to read ELF firmware: %s\n", firmware_file);
        return 1;
    }

    avr_t *avr = avr_make_mcu_by_name(mcu_name);
    if (!avr) {
        fprintf(stderr, "[ERROR] Unknown or unsupported AVR MCU: %s\n", mcu_name);
        return 1;
    }

    if (avr_init(avr) != 0) {
        fprintf(stderr, "[ERROR] Failed to initialize AVR MCU %s\n", mcu_name);
        return 1;
    }

    avr_load_firmware(avr, &firmware);
    if (frequency > 0) {
        avr->frequency = frequency;
    }

    uart_pty_t uart_pty;
    memset(&uart_pty, 0, sizeof(uart_pty));
    uart_pty_init(avr, &uart_pty);

    /* Connect to primary UART ('0' for ATmega2560/ATmega328P, '1' for ATmega32u4 hardware UART) */
    char uart_id = '0';
    if (strcmp(mcu_name, "atmega32u4") == 0) {
        uart_id = '1';
    }
    uart_pty_connect(&uart_pty, uart_id);

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    /* Notify runner of the allocated PTY slave device */
    printf("[SIMAVR] UART PTY ready on: %s\n", uart_pty.pty);
    fflush(stdout);

    while (running && avr->state != cpu_Done && avr->state != cpu_Crashed) {
        avr_run(avr);
    }

    avr_terminate(avr);
    return 0;
}
