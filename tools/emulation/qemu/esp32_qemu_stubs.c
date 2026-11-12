/**
 * @file esp32_qemu_stubs.c
 * @brief Linker stubs for running Arduino Nano ESP32 firmware in QEMU (SIL-2 /
 * Rule 15).
 *
 * In physical Arduino Nano ESP32 hardware, the board variant runs a static C++
 * constructor that manipulates external PSRAM at SOC_EXTRAM_DATA_HIGH to detect
 * reset double-taps, and calls analogWrite() to pulse the onboard RGB LED
 * before setup() is reached.
 *
 * In QEMU Xtensa ESP32-S3 emulation, external PSRAM is not mapped and the LEDC
 * APB clocks are not yet active during pre-main constructor execution. These
 * wrapper stubs bypass the hardware-specific bootloader recovery probe,
 * allowing the firmware to enter Arduino setup() and loop() cleanly.
 */

#include <stdbool.h>
#include <stdint.h>

void __wrap_double_tap_init(void) {
  /* No-op in QEMU: external PSRAM marker area is not mapped. */
}

void __wrap_double_tap_mark(void) {
  /* No-op in QEMU: double-tap recovery is not needed in automated emulation. */
}

void __wrap_double_tap_invalidate(void) { /* No-op in QEMU. */ }

bool __wrap_double_tap_check_match(void) {
  /* Always report false: proceed directly to normal user sketch execution. */
  return false;
}

void __wrap_analogWrite(uint8_t pin, int value) {
  /* No-op in QEMU: bypass pre-main LEDC hardware timer divide-by-zero. */
  (void)pin;
  (void)value;
}
