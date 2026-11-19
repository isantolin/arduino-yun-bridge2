# Pigweed upstream consolidation and firmware resource measurements

## Upstream implementation audit

The production bridge uses Pigweed's upstream `pw_rpc` APIs on the MCU, daemon,
and C++ Gateway. The AVR support repository no longer contains the former
`pw_rpc_avr` dispatcher; its README and CI audit identify upstream
`pw_rpc::Server`, `pw_rpc::Client`, and `pw_hdlc::RpcChannelOutput` as the
implementation. The bridge uses the generated Nanopb service classes and
Python's `pw_rpc.descriptors.Services` rather than a parallel service/method
registry.

`pw_transfer` is also upstream. The bridge currently pins `third_party/avr_pigweed_port` to
`026c5f6b4350e75641bc125d2098e53c7e2d317f`. The support repository's current
`main` is `ee9708e34606c0775a433aa003e5006b50c456b0`, 17 commits ahead; those
commits include a canonical idempotent Pigweed patch workflow and richer
simavr stack-low-water context. The submodule gitlink still needs to be advanced
and validated in the bridge before those newer diagnostics are available here.

The AVR-specific cooperative polling change is
a platform adaptation of Pigweed's `TransferThread`, not a second transfer
protocol or dispatcher: the AVR runtime cannot host Pigweed's blocking thread
loop, so the pinned upstream implementation exposes a bounded cooperative
poll operation. The bridge owns one upstream `Thread<1, 1>` and uses its client
and server roles sequentially. This single-role constraint is specific to the
AVR target.

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

The report intentionally leaves `runtime_stack_peak_bytes` null. Compiler
section sizes and `.su` frame reports are not a substitute for a runtime
high-water measurement. AVR runtime stack usage must be taken from the simavr
stack watermark under the relevant end-to-end workload; SAMD21 and ESP32-S3
need their own target-specific runtime watermark before a cross-architecture
stack comparison can be claimed.

## Interpreting the numbers

- The Arduino CLI summary is the cross-core build metric. It is preserved
  verbatim in `compile.log` and normalized into JSON for automation.
- AVR ELF section accounting independently checks Flash and static SRAM
  consumption after link-time optimization.
- Static SRAM is not free stack. The remaining bytes are shared by heap and
  stack; a runtime stack watermark is required to establish the actual margin.
- To quantify the incremental cost of upstream `pw_transfer`, compare the
  same Mega 2560 sketch, toolchain, and build flags with
  `BRIDGE_ENABLE_PW_TRANSFER=1` and `=0`. Do not use a standalone port probe
  as a substitute for the complete Bridge firmware.
- Never extrapolate AVR resource costs to SAMD21 or ESP32-S3. Each architecture
  must be compiled and measured with its own toolchain and configuration.

No measured values are hard-coded in this document. The JSON artifacts from
the final CI head are the source of truth for the actual build.
