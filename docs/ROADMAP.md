# Roadmap

> **Current Release**: v2.0.1 (OpenWrt 25.12.0 compatible)

## Completed

- **Extended Metrics** ✅ (Jan 2026): Added comprehensive observability metrics:
  - `SerialLatencyStats`: RPC command latency histogram with configurable buckets
  - `SerialThroughputStats`: Serial link bytes/frames sent/received counters
  - `queue_depths`: Real-time queue monitoring (MQTT, console, mailbox, pending reads, processes)
  - Prometheus histogram export for Grafana dashboards (`yunbridge_serial_rpc_latency_seconds`)

- **Performance Tuning** ✅ (Jan 2026): Analysis completed
  - Dispatcher architecture already optimized with handler registry pattern
  - No unnecessary allocations in hot path
  - Serial flow optimized with static buffers

## Pending (Requires Hardware)

- **Hardware Verification**: Conduct extensive testing on physical Arduino Yun hardware.
- **ESP32/ESP8266 Validation**: Test multi-platform watchdog support on real hardware.
