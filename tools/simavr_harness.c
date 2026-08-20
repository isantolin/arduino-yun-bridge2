/*
 * simavr_harness.c - Cycle-Accurate AVR Hardware Simulation Harness with PTY UART Bridge
 *
 * Implements a self-contained PTY bridge using standard libsimavr core API
 * (sim_avr.h, sim_elf.h, avr_uart.h) without external header dependencies.
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <libgen.h>
#include <pty.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

#include <simavr/avr_uart.h>
#include <simavr/sim_avr.h>
#include <simavr/sim_elf.h>
#include <simavr/sim_io.h>

static volatile int running = 1;

static void sig_handler(int sig) {
    (void)sig;
    running = 0;
}

typedef struct {
    int master_fd;
    int slave_fd;
    char slave_name[128];
    avr_t *avr;
    char uart_id;
} simavr_pty_bridge_t;

static void uart_output_hook(struct avr_irq_t *irq, uint32_t value, void *param) {
    (void)irq;
    simavr_pty_bridge_t *b = (simavr_pty_bridge_t *)param;
    uint8_t byte = (uint8_t)value;
    (void)write(b->master_fd, &byte, 1);
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

    simavr_pty_bridge_t bridge;
    memset(&bridge, 0, sizeof(bridge));
    bridge.avr = avr;
    bridge.uart_id = (strcmp(mcu_name, "atmega32u4") == 0) ? '1' : '0';

    if (openpty(&bridge.master_fd, &bridge.slave_fd, bridge.slave_name, NULL, NULL) < 0) {
        perror("[ERROR] openpty failed");
        return 1;
    }

    /* Set raw mode on PTY */
    struct termios tio;
    if (tcgetattr(bridge.master_fd, &tio) == 0) {
        cfmakeraw(&tio);
        tcsetattr(bridge.master_fd, TCSANOW, &tio);
    }

    /* Set non-blocking read on master FD */
    int flags = fcntl(bridge.master_fd, F_GETFL, 0);
    fcntl(bridge.master_fd, F_SETFL, flags | O_NONBLOCK);

    /* Hook UART output from AVR to PTY master */
    avr_irq_t *uart_out_irq = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ(bridge.uart_id), UART_IRQ_OUTPUT);
    if (uart_out_irq) {
        avr_irq_register_notify(uart_out_irq, uart_output_hook, &bridge);
    }

    avr_irq_t *uart_in_irq = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ(bridge.uart_id), UART_IRQ_INPUT);

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    printf("[SIMAVR] UART PTY ready on: %s\n", bridge.slave_name);
    fflush(stdout);

    uint8_t rx_buf[128];
    while (running && avr->state != cpu_Done && avr->state != cpu_Crashed) {
        /* Read incoming bytes from PTY master and inject into AVR UART input IRQ */
        ssize_t n = read(bridge.master_fd, rx_buf, sizeof(rx_buf));
        if (n > 0 && uart_in_irq) {
            for (ssize_t i = 0; i < n; i++) {
                avr_raise_irq(uart_in_irq, rx_buf[i]);
            }
        }

        avr_run(avr);
    }

    close(bridge.master_fd);
    close(bridge.slave_fd);
    avr_terminate(avr);
    return 0;
}
