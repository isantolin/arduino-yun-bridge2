# Pigweed upstream consolidation and firmware resource measurements

## Upstream implementation audit

The production bridge uses Pigweed's upstream `pw_rpc` APIs on the MCU, daemon,
and C++ Gateway. The AVR support repository no longer contains the former
`pw_rpc_avr` dispatcher; its README and CI audit identify upstream
`pw_rpc::Server`, `pw_rpc::Client`, and `pw_hdlc::RpcChannelOutput` as the
implementation. The bridge uses the generated Nanopb service classes and
Python's `pw_rpc.descriptors.Services` rather than a parallel service/method
registry.

`pw_transfer` is also upstream. The repository is already pinned to
`third_party/avr_pigweed_port` commit
`ee9708e34606c0775a433aa003e5006b50c456b0`. That revision includes the
idempotent Pigweed patch workflow and simavr stack-low-water context. This PR
does not add further `pw_emu` integration; it only consumes the existing
stack instrumentation for resource measurements.

The cooperative polling adaptation is
a platform adaptation of Pigweed's `TransferThread`, not a second transfer
protocol or dispatcher: the bare-metal Arduino loop cannot host Pigweed's
blocking thread loop, so the pinned upstream implementation exposes a bounded
cooperative poll operation (`_transfer_thread.Poll(2)`). The bridge owns one
upstream `Thread<1, 1>` and uses its client and server roles sequentially
across AVR Mega 2560, SAMD21 MKR WiFi 1010, and ESP32-S3 Nano ESP32.

Application-specific code remains necessary at real system boundaries:

- `EepromStream` maps a fixed-size Pigweed stream to the AVR EEPROM and protects
  the reserved boot-epoch bytes.
- The upload resource exposes an application-owned buffer with a bounded
  lifetime.
- Linux transfer handlers map Pigweed resources to bounded staging files and
  enforce path, quota, and authorization policy.
- Serial, HDLC, AEAD, and asyncio adapters connect Pigweed to the hardware and
  daemon runtime.

These are resource and security backends, not replacements for `pw_rpc` or
`pw_transfer`. Do not remove them merely to reduce line count.

## Automated build measurements

`tools/ci/compile_firmware.sh` now writes `resource_report.json` beside each
compiled firmware image. The report records the Arduino core's measured:

- Flash usage and target limit;
- static/global RAM usage, remaining RAM, and target limit.

For AVR builds it additionally records `.text`, `.data`, `.bss`, and `.noinit`
from `avr-size -A` on the linked ELF. The report is uploaded with the
`arduino-logs` CI artifact for the Mega 2560 BridgeControl firmware and the
dedicated upstream `pw_transfer` API probe.

The static `resource_report.json` intentionally leaves
`runtime_stack_peak_bytes` null because it is generated before execution.
Runtime stack watermarks are measured during actual emulator execution across
all three architectures:

- **AVR Mega 2560 (`simavr`):** `simavr_runner.py` writes
  `simavr-logs/arduino-avr-mega_stack_watermark.json` using the cycle-accurate
  low-water SP and peak-context records emitted by `simavr_pty_runner`.
- **SAMD21 MKR WiFi 1010 (`Renode`):** `bridge::hal::initStackCanary()` paints
  the free SRAM region between `__bss_end__` and the initial stack pointer with
  `0xA5` at startup, and `bridge::hal::getStackWatermark()` scans the untouched
  prefix against `__StackTop - __bss_end__`. `BridgeClass` emits changes over
  `HDLC_ADDRESS_LOG`, and `renode_runner.py` writes
  `renode-logs/arduino-samd-mkrwifi1010_stack_watermark.json`.
- **ESP32-S3 Nano ESP32 (`QEMU`):** `bridge::hal::getStackWatermark()` queries
  FreeRTOS `uxTaskGetStackHighWaterMark(nullptr)` on the Arduino `loopTask`
  against `getArduinoLoopTaskStackSize()`. `BridgeClass` emits changes over
  `HDLC_ADDRESS_LOG`, and `qemu_runner.py` writes
  `qemu-logs/esp32-esp32-nano_nora_stack_watermark.json`.

## Interpreting the numbers

- The Arduino CLI summary is the cross-core build metric. It is preserved
  verbatim in `compile.log` and normalized into JSON for automation.
- AVR ELF section accounting independently checks Flash and static SRAM
  consumption after link-time optimization.
- Static SRAM is not free stack. The remaining bytes are shared by heap and
  stack; a runtime stack watermark is required to establish the actual margin.
- Measure only the canonical firmware build, with upstream `pw_transfer`
  always present. Report total Flash, static SRAM, and runtime stack watermark
  by target; do not create a disabled-transfer build or infer a removable
  feature cost from a second product configuration.
- Never extrapolate AVR resource costs to SAMD21 or ESP32-S3. Each architecture
  must be compiled and measured with its own toolchain and configuration.

No measured values are hard-coded in this document. The JSON artifacts from
the final CI head are the source of truth for the actual build.
