/*
 * simavr_harness.cpp - Cycle-Accurate AVR Hardware Simulation Harness (SIL-2 / ETL-compliant)
 *
 * Bridges simulated AVR microcontroller (ATmega32u4 / ATmega2560 / ATmega328P) UART
 * to a Linux Pseudo-Terminal (PTY) using zero-heap ETL data structures, STL-free algorithms,
 * cycle-accurate UART ISR pacing, and deterministic RAII lifecycle management.
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
#include <termios.h>
#include <unistd.h>

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>
#include <etl/string_view.h>

extern "C" {
#if __has_include(<simavr/avr_uart.h>)
#include <simavr/avr_uart.h>
#include <simavr/sim_avr.h>
#include <simavr/sim_elf.h>
#include <simavr/sim_io.h>
#elif __has_include(<avr_uart.h>)
#include <avr_uart.h>
#include <sim_avr.h>
#include <sim_elf.h>
#include <sim_io.h>
#else
// Declarations for environments without libsimavr-dev headers installed
enum { cpu_Done = 2, cpu_Crashed = 3 };
enum { UART_IRQ_INPUT = 0, UART_IRQ_OUTPUT = 1 };
#define AVR_IOCTL_UART_GETIRQ(name) (0x10000 | (name))

typedef struct elf_firmware_t {
    uint32_t frequency;
} elf_firmware_t;

typedef struct avr_irq_t avr_irq_t;
typedef void (*avr_irq_notify_t)(struct avr_irq_t *irq, uint32_t value, void *param);

typedef struct avr_t {
    uint32_t frequency;
    int state;
} avr_t;

int elf_read_firmware(const char *file, elf_firmware_t *firmware);
avr_t *avr_make_mcu_by_name(const char *name);
int avr_init(avr_t *avr);
void avr_load_firmware(avr_t *avr, elf_firmware_t *firmware);
avr_irq_t *avr_io_getirq(avr_t *avr, uint32_t ctl, int index);
void avr_irq_register_notify(avr_irq_t *irq, avr_irq_notify_t notify, void *param);
void avr_raise_irq(avr_irq_t *irq, uint32_t value);
int avr_run(avr_t *avr);
void avr_terminate(avr_t *avr);
#endif
}

namespace {

constexpr size_t RX_BUFFER_CAPACITY = 256;
constexpr size_t PTY_NAME_CAPACITY = 128;
constexpr uint32_t DEFAULT_AVR_FREQUENCY = 16000000UL;
constexpr size_t BATCH_INSTRUCTION_CYCLES = 50000;
constexpr size_t INTER_BYTE_ISR_CYCLES = 4000;

volatile sig_atomic_t g_running = 1;

void sig_handler(int /* sig */) noexcept {
    g_running = 0;
}

class SimavrHardwareBridge {
public:
    SimavrHardwareBridge() noexcept = default;

    ~SimavrHardwareBridge() noexcept {
        release();
    }

    SimavrHardwareBridge(const SimavrHardwareBridge &) = delete;
    SimavrHardwareBridge &operator=(const SimavrHardwareBridge &) = delete;

    bool initialize(etl::string_view firmware_path, etl::string_view mcu_name, uint32_t frequency, char uart_id = '\0') noexcept {
        elf_firmware_t firmware{};
        if (elf_read_firmware(firmware_path.data(), &firmware) != 0) {
            fprintf(stderr, "[ERROR] Failed to read ELF firmware: %s\n", firmware_path.data());
            return false;
        }

        avr_ = avr_make_mcu_by_name(mcu_name.data());
        if (!avr_) {
            fprintf(stderr, "[ERROR] Unknown or unsupported AVR MCU: %s\n", mcu_name.data());
            return false;
        }

        if (avr_init(avr_) != 0) {
            fprintf(stderr, "[ERROR] Failed to initialize AVR MCU %s\n", mcu_name.data());
            return false;
        }

        avr_load_firmware(avr_, &firmware);
        if (frequency > 0) {
            avr_->frequency = frequency;
        }

        if (uart_id >= '0' && uart_id <= '3') {
            uart_id_ = uart_id;
        } else {
            uart_id_ = (mcu_name == "atmega32u4") ? '1' : '0';
        }

        if (openpty(&master_fd_, &slave_fd_, slave_name_.data(), nullptr, nullptr) < 0) {
            perror("[ERROR] openpty failed");
            return false;
        }

        struct termios tio{};
        if (tcgetattr(master_fd_, &tio) == 0) {
            cfmakeraw(&tio);
            tcsetattr(master_fd_, TCSANOW, &tio);
        }

        const int flags = fcntl(master_fd_, F_GETFL, 0);
        fcntl(master_fd_, F_SETFL, flags | O_NONBLOCK);

        avr_irq_t *uart_out_irq = avr_io_getirq(avr_, AVR_IOCTL_UART_GETIRQ(uart_id_), UART_IRQ_OUTPUT);
        if (uart_out_irq) {
            avr_irq_register_notify(uart_out_irq, &SimavrHardwareBridge::uart_output_hook, this);
        }

        uart_in_irq_ = avr_io_getirq(avr_, AVR_IOCTL_UART_GETIRQ(uart_id_), UART_IRQ_INPUT);
        return true;
    }

    void run_simulation() noexcept {
        printf("[SIMAVR] UART%c PTY ready on: %s\n", uart_id_, slave_name_.data());
        fflush(stdout);

    simulation_step:
        if (!is_active()) {
            return;
        }
        execute_cycle_batch();
        goto simulation_step;
    }

    [[nodiscard]] bool is_active() const noexcept {
        return g_running != 0 && avr_ != nullptr && avr_->state != cpu_Done && avr_->state != cpu_Crashed;
    }

    void release() noexcept {
        if (master_fd_ >= 0) {
            close(master_fd_);
            master_fd_ = -1;
        }
        if (slave_fd_ >= 0) {
            close(slave_fd_);
            slave_fd_ = -1;
        }
        if (avr_ != nullptr) {
            avr_terminate(avr_);
            avr_ = nullptr;
        }
    }

private:
    static void uart_output_hook(struct avr_irq_t * /* irq */, uint32_t value, void *param) noexcept {
        auto *self = static_cast<SimavrHardwareBridge *>(param);
        if (self && self->master_fd_ >= 0) {
            const uint8_t byte = static_cast<uint8_t>(value);
            const ssize_t written = write(self->master_fd_, &byte, 1);
            (void)written;
        }
    }

    void execute_cycle_batch() noexcept {
        /* Read incoming bytes from PTY master */
        const ssize_t bytes_read = read(master_fd_, rx_buf_.data(), rx_buf_.size());
        if (bytes_read > 0 && uart_in_irq_ != nullptr) {
            /* Paced UART delivery: step CPU between each byte so the AVR UART RX ISR executes without overrun */
            etl::for_each(rx_buf_.begin(), rx_buf_.begin() + bytes_read, [this](uint8_t byte) {
                avr_raise_irq(uart_in_irq_, byte);
                etl::for_each(inter_byte_batch_.begin(), inter_byte_batch_.end(), [this](int & /* step */) {
                    if (avr_ && avr_->state != cpu_Done && avr_->state != cpu_Crashed) {
                        avr_run(avr_);
                    }
                });
            });
        }

        /* Execute standard instruction batch for main loop processing */
        etl::for_each(step_batch_.begin(), step_batch_.end(), [this](int & /* step */) {
            if (avr_ && avr_->state != cpu_Done && avr_->state != cpu_Crashed) {
                avr_run(avr_);
            }
        });
    }

    int master_fd_{-1};
    int slave_fd_{-1};
    etl::array<char, PTY_NAME_CAPACITY> slave_name_{};
    avr_t *avr_{nullptr};
    char uart_id_{'0'};
    avr_irq_t *uart_in_irq_{nullptr};
    etl::array<uint8_t, RX_BUFFER_CAPACITY> rx_buf_{};
    etl::array<int, BATCH_INSTRUCTION_CYCLES> step_batch_{};
    etl::array<int, INTER_BYTE_ISR_CYCLES> inter_byte_batch_{};
};

}  // namespace

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <firmware.elf> [mcu] [frequency_hz] [uart_id]\n", argv[0]);
        return 1;
    }

    const etl::string_view firmware_file = argv[1];
    const etl::string_view mcu_name = (argc > 2) ? argv[2] : "atmega2560";
    const uint32_t frequency = (argc > 3) ? static_cast<uint32_t>(strtoul(argv[3], nullptr, 10)) : DEFAULT_AVR_FREQUENCY;
    const char uart_id = (argc > 4) ? argv[4][0] : '\0';

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    SimavrHardwareBridge bridge;
    if (!bridge.initialize(firmware_file, mcu_name, frequency, uart_id)) {
        return 1;
    }

    bridge.run_simulation();
    return 0;
}
