/*
 * simavr_harness.cpp - Cycle-Accurate AVR Hardware Simulation Harness (SIL-2 / ETL-compliant)
 *
 * Links simulated AVR microcontroller (ATmega32u4 / ATmega2560 / ATmega328P) UART
 * to a Linux Pseudo-Terminal (PTY) using zero-heap ETL data structures and STL-free algorithms.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <fcntl.h>
#include <libgen.h>
#include <pty.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

#include <etl/algorithm.h>
#include <etl/array.h>

extern "C" {
#include <simavr/avr_uart.h>
#include <simavr/sim_avr.h>
#include <simavr/sim_elf.h>
#include <simavr/sim_io.h>
}

namespace {

constexpr size_t RX_BUFFER_CAPACITY = 128;
constexpr size_t PTY_NAME_CAPACITY = 128;
constexpr uint32_t DEFAULT_AVR_FREQUENCY = 16000000UL;
constexpr int BATCH_INSTRUCTION_CYCLES = 1000;

volatile sig_atomic_t g_running = 1;

void sig_handler(int /* sig */) noexcept {
    g_running = 0;
}

struct SimavrPtyBridge {
    int master_fd{-1};
    int slave_fd{-1};
    etl::array<char, PTY_NAME_CAPACITY> slave_name{};
    avr_t *avr{nullptr};
    char uart_id{'0'};
};

void uart_output_hook(struct avr_irq_t * /* irq */, uint32_t value, void *param) noexcept {
    auto *bridge = static_cast<SimavrPtyBridge *>(param);
    if (!bridge || bridge->master_fd < 0) {
        return;
    }
    const uint8_t byte = static_cast<uint8_t>(value);
    const ssize_t written = write(bridge->master_fd, &byte, 1);
    (void)written;
}

}  // namespace

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <firmware.elf> [mcu] [frequency_hz]\n", argv[0]);
        return 1;
    }

    const char *firmware_file = argv[1];
    const char *mcu_name = (argc > 2) ? argv[2] : "atmega2560";
    const uint32_t frequency = (argc > 3) ? static_cast<uint32_t>(strtoul(argv[3], nullptr, 10)) : DEFAULT_AVR_FREQUENCY;

    elf_firmware_t firmware{};
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

    SimavrPtyBridge bridge{};
    bridge.avr = avr;
    bridge.uart_id = (strcmp(mcu_name, "atmega32u4") == 0) ? '1' : '0';

    if (openpty(&bridge.master_fd, &bridge.slave_fd, bridge.slave_name.data(), nullptr, nullptr) < 0) {
        perror("[ERROR] openpty failed");
        return 1;
    }

    /* Set raw mode on PTY */
    struct termios tio{};
    if (tcgetattr(bridge.master_fd, &tio) == 0) {
        cfmakeraw(&tio);
        tcsetattr(bridge.master_fd, TCSANOW, &tio);
    }

    /* Set non-blocking read on master FD */
    const int flags = fcntl(bridge.master_fd, F_GETFL, 0);
    fcntl(bridge.master_fd, F_SETFL, flags | O_NONBLOCK);

    /* Hook UART output from AVR to PTY master */
    avr_irq_t *uart_out_irq = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ(bridge.uart_id), UART_IRQ_OUTPUT);
    if (uart_out_irq) {
        avr_irq_register_notify(uart_out_irq, uart_output_hook, &bridge);
    }

    avr_irq_t *uart_in_irq = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ(bridge.uart_id), UART_IRQ_INPUT);

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    printf("[SIMAVR] UART PTY ready on: %s\n", bridge.slave_name.data());
    fflush(stdout);

    etl::array<uint8_t, RX_BUFFER_CAPACITY> rx_buf{};
    while (g_running && avr->state != cpu_Done && avr->state != cpu_Crashed) {
        /* Read incoming bytes from PTY master and inject into AVR UART input IRQ using ETL algorithms */
        const ssize_t bytes_read = read(bridge.master_fd, rx_buf.data(), rx_buf.size());
        if (bytes_read > 0 && uart_in_irq) {
            etl::for_each(rx_buf.begin(), rx_buf.begin() + bytes_read, [uart_in_irq](uint8_t byte) {
                avr_raise_irq(uart_in_irq, byte);
            });
        }

        /* Execute AVR instructions in batches using ETL iteration */
        etl::array<int, BATCH_INSTRUCTION_CYCLES> step_batch{};
        etl::for_each(step_batch.begin(), step_batch.end(), [avr](int & /* step */) {
            if (avr->state != cpu_Done && avr->state != cpu_Crashed) {
                avr_run(avr);
            }
        });
        usleep(100);
    }

    close(bridge.master_fd);
    close(bridge.slave_fd);
    avr_terminate(avr);
    return 0;
}
