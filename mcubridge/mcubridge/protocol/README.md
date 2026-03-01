# Protocol Definition Layer

This directory contains the **Source of Truth** for the MCU Bridge Protocol.

## Architecture & Layering

The system is designed with a strict layering to separate the wire protocol from the application logic.

### 1. The Protocol Contract (`protocol/protocol.py`)
- **Role:** Low-level Protocol Constants.
- **Content:** Command IDs, Status Codes, Binary Format definitions, Magic Numbers.
- **Origin:** Auto-generated from `tools/protocol/spec.toml` using `tools/protocol/generate.py`.
- **Modification Policy:** **DO NOT EDIT MANUALLY.**

### 2. The Data Structures (`protocol/structures.py`)
- **Role:** Typed Single Source of Truth.
- **Content:** Msgspec/Construct hybrid structures for all RPC packets.
- **Benefit:** Provides automatic validation, binary parsing, and high-performance serialization.

### 3. The Application Defaults (`mcubridge.config.const`)
- **Role:** Application Configuration Defaults.
- **Content:** OS-specific limits, MQTT topics, and paths.

### 3. The Runtime Configuration (`mcubridge.config.settings`)
- **Role:** Operational State.
- **Content:** Final values loaded from OpenWrt's UCI system (`/etc/config/mcubridge`), Environment Variables, or `mcubridge.const` defaults.
- **Logic:** Validates types, ranges, and sanity of the configuration before the Daemon starts.

## Authoritative Usage

To ensure consistency and avoid "Magic Numbers":
- **Protocol Constants:** Must be imported from `mcubridge.protocol.protocol`.
- **App Defaults:** Must be imported from `mcubridge.const`.
