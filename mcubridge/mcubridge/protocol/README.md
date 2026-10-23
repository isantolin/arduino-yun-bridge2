# Protocol Definition Layer

This directory contains the **Source of Truth** for the MCU Bridge Protocol.

## Architecture & Layering

The system is designed with a strict layering to separate the wire protocol from the application logic.

### 1. The Protocol Contract & Runtime SSOT (`protocol/protocol.py`)
- **Role:** Protocol Constants and Centralized Single Source of Truth (`REGISTRY`).
- **Content:** Command IDs, Status Codes, Binary Format definitions, Magic Numbers, and the unified immutable `REGISTRY` (`OperationSpec`) with $O(1)$ dispatch tables and topic resolution.
- **Origin:** Auto-generated from `tools/protocol/mcubridge.proto` using `tools/protocol/generate.py`.
- **Modification Policy:** **DO NOT EDIT MANUALLY.**

### 2. The Protobuf Runtime & Structures (`protocol/mcubridge_pb2.py` & `protocol/structures.py`)
- **Role:** Typed Protobuf Bindings and Security/Policy Helpers.
- **Content:** Native `google.protobuf` message classes, binary parsing utilities, and policy validation functions (`is_command_allowed`).
- **Benefit:** Provides compile-time typing, binary parsing, and high-performance serialization via `google.protobuf`.

### 3. The Application Defaults (`mcubridge.config.const`)
- **Role:** Application Configuration Defaults.
- **Content:** OS-specific limits, Cloud topics, and paths.

### 3. The Runtime Configuration (`mcubridge.config.settings`)
- **Role:** Operational State.
- **Content:** Final values loaded from OpenWrt's UCI system (`/etc/config/mcubridge`), Environment Variables, or `mcubridge.const` defaults.
- **Logic:** Validates types, ranges, and sanity of the configuration before the Daemon starts.

## Authoritative Usage

To ensure consistency and avoid "Magic Numbers":
- **Protocol Constants:** Must be imported from `mcubridge.protocol.protocol`.
- **App Defaults:** Must be imported from `mcubridge.const`.
