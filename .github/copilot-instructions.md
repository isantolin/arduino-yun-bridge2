# Copilot instructions

Use these guidelines when working in this repository. Follow the user's request and the active environment's tool and safety requirements; do not assume Gemini-specific workflows or unavailable tools.

## Understand the change

- Inspect the relevant implementation, tests, and configuration before editing. Treat repository documentation as context, and verify details against the current source and configuration when they disagree.
- Keep changes focused. Preserve existing public APIs and wire-protocol compatibility unless the request explicitly calls for a change.
- Do not broaden a task into unrelated refactoring or enforce repository-wide audits for a narrowly scoped change.

## Repository conventions

- Python runtime and tooling requirements are defined in `pyproject.toml` and `tox.ini`. Follow the configured formatting, lint, and strict type-checking rules.
- The Python daemon, gateway, client examples, Arduino library, LuCI application, and shared tools live in `mcubridge/`, `mcubridge-gateway/`, `mcubridge-client-examples/`, `mcubridge-library-arduino/`, `luci-app-mcubridge/`, and `tools/`.
- When changing data shared across the MCU/MPU protocol, update the canonical schema in `tools/protocol/mcubridge.proto` and its generated outputs using the repository's documented generator. Add or update relevant tests.
- Follow the allocation, timing, and toolchain constraints of the specific Arduino target when changing firmware. Check the applicable build configuration rather than assuming one constraint applies to every target.

## Validation

- Add or update tests for behavior changes. Do not weaken or remove tests to make a change pass.
- Run the existing, relevant tests and checks defined by `tox.ini` or the affected component's documentation. Run broader checks when the change affects shared behavior.
- If a check requires unavailable hardware, services, or tooling, say so clearly and report what was validated instead.

## Safety and security

- Preserve protocol validation, authentication, cryptographic, and error-handling behavior unless explicitly changing it.
- Do not add secrets, suppress diagnostics, or silently ignore errors.
- Treat external input as untrusted and validate it at the relevant boundary.
