# Protocol Definition Layer

This directory contains the **Source of Truth** for the MCU Bridge Protocol.

## Architecture & Layering

The system is designed with a strict layering to separate the wire protocol from the application logic.

### 1. The Protocol Contract (`rpc/protocol.py`)
- **Role:** Source of Truth.
- **Content:** Command IDs, Status Codes, Binary Format definitions, Magic Numbers.
- **Origin:** Auto-generated from `openwrt-library-arduino/src/protocol/rpc_protocol.h` (or sharing the same generator source).
- **Modification Policy:** **DO NOT EDIT MANUALLY.** Changes here must be synchronized with the C++ library to ensure binary compatibility.

### 2. The Application Defaults (`mcubridge.const`)
- **Role:** Application Configuration Defaults & Environment Variables.
- **Content:** Application-specific defaults (File paths, OS limits, MQTT topics) and Environment Variable names.
- **Relation to Protocol:** It **does not** mirror protocol constants. Code requiring protocol constants (like Frame Delimiters or Command IDs) must import them directly from `mcubridge.protocol.protocol`.

### 3. The Runtime Configuration (`mcubridge.config.settings`)
- **Role:** Operational State.
- **Content:** Final values loaded from OpenWrt's UCI system (`/etc/config/mcubridge`), Environment Variables, or `mcubridge.const` defaults.
- **Logic:** Validates types, ranges, and sanity of the configuration before the Daemon starts.

## Authoritative Usage

To ensure consistency and avoid "Magic Numbers":
- **Protocol Constants:** Must be imported from `mcubridge.protocol.protocol`.
- **App Defaults:** Must be imported from `mcubridge.const`.
