# YunBridge Library Layout

This documentation outlines the internal structure of the YunBridge Arduino library.

## Source Tree

- `src/arduino/` – runtime classes that interact directly with Arduino APIs (e.g. `Bridge.cpp`).
- `src/protocol/` – protocol helpers shared with the Linux daemon (COBS, CRC, frame builders).
- `src/` – public headers exported to sketches (`Bridge.h`, `Console.h`, etc.).
- `examples/` – ready-to-upload sketches demonstrating the library (e.g. `BridgeControl`).
- `docs/` – additional documentation and protocol references (`PROTOCOL.md`).
- `tools/` – install scripts or build helpers (currently `install.sh`).

### Actualizaciones recientes

- `Bridge.cpp` implementa colas de seguimiento para operaciones de datastore y mailbox, asegurando que las confirmaciones lleguen al daemon con el formato esperado.
- Los helpers de proceso mantienen buffers parciales para que las respuestas de `processPoll` coincidan con las garantías descritas en `PROTOCOL.md`.

Keeping the protocol and Arduino-specific code separated clarifies ownership and reuse, while the examples directory mirrors Arduino Library Manager conventions.
