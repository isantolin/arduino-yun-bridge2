---
name: protobuf-protocol-change
description: Change or audit Protobuf schemas, generated bindings, RPC messages, framing, or MCU/MPU protocol contracts. Use when protocol definitions or consumers change.
---

# Protobuf Protocol Change

## Canonical sources

The canonical schema files are under tools/protocol/: config_schema.proto, mcu_wire.proto, and cloud_gateway.proto. Verify the current tree and actual generated-output locations. Follow the repository's generator; never hand-edit generated bindings when a canonical schema exists.

## Impact analysis

1. Identify changed messages, enums, services, fields, options, and constants.
2. Trace producers and consumers: firmware/library, Python daemon, gateway, clients, RPC dispatch, LuCI where applicable, tests, generated bindings, packaging, and CI.
3. Determine effects on wire encoding, semantics, validation, frame limits, authentication, retries, errors, and deployment compatibility.
4. Check duplicate/stale schemas, generated-code drift, unused definitions, and constants duplicated outside their source of truth.

## Change rules

- Use native Protobuf/Nanopb APIs and generated types. Do not manually recreate serialization, parsing, copying, or dispatch behavior already provided by the libraries.
- Preserve wire contracts unless the user explicitly approves a protocol change.
- Do not add reserved fields/numbers merely as cleanup. Follow repository migration policy and the user's explicit direction.
- Verify field types, defaults, validation, message bounds, Nanopb static sizes, enum behavior, and version/handshake negotiation.
- For MCU changes, account for RAM/Flash, stack, CPU, ISR constraints, and supported boards.
- For gateway changes, verify generated service interfaces and both server/client paths.

## Validation

Run the canonical schema generator/linter, protocol integrity audits, focused tests for affected producers/consumers and malformed inputs, and relevant firmware/Python/gateway checks. Inspect generated diffs; unexpected large changes require explanation.

## Completion report

Summarize schema changes, regenerated artifacts, affected consumers, compatibility consequences, tests, builds/emulation, and deployment coordination.
