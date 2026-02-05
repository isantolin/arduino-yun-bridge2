# McuBridge Library Layout

This documentation outlines the internal structure of the McuBridge Arduino library.

## Source Tree

- `src/services/` – runtime classes that interact directly with Arduino APIs (e.g. `Bridge.cpp`).
- `src/fsm/` – **ETL-based finite state machine** (`bridge_fsm.h`) implementing SIL-2 compliant state transitions.
- `src/protocol/` – protocol helpers shared with the Linux daemon (COBS, CRC, frame builders).
- `src/` – public headers exported to sketches (`Bridge.h`, `Console.h`, etc.).
- `examples/` – ready-to-upload sketches demonstrating the library (e.g. `BridgeControl`).
- `docs/` – additional documentation and protocol references (`PROTOCOL.md`).
- `tools/` – install scripts or build helpers (currently `install.sh`).

### Actualizaciones recientes

- `Bridge.cpp` implementa colas de seguimiento para operaciones de datastore y mailbox, asegurando que las confirmaciones lleguen al daemon con el formato esperado.
- Los helpers de proceso mantienen buffers parciales para que las respuestas de `processPoll` coincidan con las garantías descritas en `PROTOCOL.md`.
- Cada sketch debe definir `BRIDGE_SERIAL_SHARED_SECRET` (por ejemplo `#define BRIDGE_SERIAL_SHARED_SECRET "..."`) antes de incluir `Bridge.h`; ese valor debe coincidir con el secreto configurado en el daemon.
- Para recolectar cobertura en la librería desde un entorno host, ejecuta `./tools/coverage_arduino.sh`. El script compila automáticamente un harness de protocolo con `g++ -fprofile-arcs -ftest-coverage` contra los stubs de `tools/arduino_stub`, ejecuta el binario y genera reportes HTML/XML bajo `coverage/arduino/`. Si prefieres usar tus propios artefactos instrumentados (por ejemplo, compilados con `arduino-cli`), pasa `--build-dir` y `--output-root` antes de lanzarlo.

Keeping the protocol and Arduino-specific code separated clarifies ownership and reuse, while the examples directory mirrors Arduino Library Manager conventions.
