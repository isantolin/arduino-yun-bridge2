---
name: protobuf-protocol-change
description: Change or audit Protobuf schemas, generated bindings, RPC messages, dispatch, or MCU-to-MPU protocol behavior. Use whenever protocol structure or wire communication changes.
---

# Protobuf Protocol Change

Canonical schemas:
- tools/protocol/config_schema.proto
- tools/protocol/mcu_wire.proto
- tools/protocol/cloud_gateway.proto

Inspect imports, options, custom descriptors, generators, and consumers before editing. Do not invent a monolithic mcubridge.proto path or edit generated bindings as the source of truth.

## Required impact trace

1. Identify affected message, enum, field, option, command, and direction.
2. Trace schema -> generated Python/C++ bindings -> firmware encoder/decoder -> MPU daemon -> gateway -> clients/examples -> tests.
3. Identify producers/consumers, command IDs, dispatch tables, validation, payload bounds, error responses, and capability/version checks.
4. Determine wire compatibility and mixed-version/partial-upgrade behavior. Never claim compatibility without checking wire semantics and consumers.
5. Check whether removed/changed fields are used by generated code, persisted data, clients, or integrations.
6. Update canonical schemas and regenerate outputs. Inspect tools/protocol/generate.py and tox.ini before running the generator.
7. Update functional, malformed-input, boundary, symmetry, and regression tests. Verify both encoding and decoding.
8. Run protocol generation/checks and relevant Python, C++, emulator, and CI validation.

Use Protobuf/Nanopb generated APIs directly where appropriate. Do not duplicate generated parsing, serialization, descriptors, metadata, or dispatch. Keep one canonical definition per contract. Do not reserve removed field numbers/names unless an external contract or user requires it. Validate payload size, enum domains, and malformed/truncated frames at the correct boundary.

Report schemas, generated outputs, consumers covered, compatibility decision, tests, and deployment risks.