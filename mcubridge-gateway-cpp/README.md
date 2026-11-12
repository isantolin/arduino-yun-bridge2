# Standalone Pigweed Linux Gateway

This directory is the replacement service required by ADR-0003. It is a
standalone Linux C++ process, not a sidecar to the Python Gateway.

## Transport contract

- TLS 1.3 with mandatory client-certificate verification.
- Device identity is accepted only from one URI SAN:
  spiffe://mcubridge/device/DEVICE_ID.
- Northbound client identity is accepted only from one URI SAN:
  spiffe://mcubridge/client/PRINCIPAL_ID.
- Common Name and client-supplied device identifiers are never identity sources.
- Device and client principals use separate session registries; duplicate
  sessions for the same principal are rejected.
- Only client-role sessions register the northbound GatewayControl service.
  Its current Health RPC reports readiness and active session counts.
- Pigweed RPC packets use upstream pw_hdlc framing on HDLC address 1.
- The upstream pw_transfer::TransferService is registered on the per-peer
  upstream pw_rpc::Server.
- Each authenticated peer owns its RPC channel and transport buffers.
  Device sessions additionally own a transfer thread and TransferService.
  Session teardown terminates and joins the device transfer worker.
- TX framing is serialized and passed through a bounded queue; a full queue
  fails the send with RESOURCE_EXHAUSTED rather than dropping bytes.

The transfer service is intentionally registered without file handlers in
this first service slice. GatewayControl currently exposes only Health; it is
not yet a replacement for telemetry, commands, console, mailbox or sessions.
Filesystem handlers must be attached only after the MPU resource broker
enforces per-device roots, path confinement, quotas, seek/resume and
atomic-write rules. No arbitrary host path is exposed.

## Build

Pigweed source is the recursively initialized submodule already pinned by
third_party/avr_pigweed_port; it is not downloaded at an independent revision.

On Debian/Ubuntu host:

    sudo apt-get install -y build-essential cmake ninja-build libssl-dev protobuf-compiler python3
    cmake -S mcubridge-gateway-cpp -B build/mcubridge-gateway -G Ninja
    cmake --build build/mcubridge-gateway

The project explicitly selects POSIX/STL Pigweed backends. It does not reuse
the AVR CMake toolchain or the AVR cooperative transfer-thread backend.

## Run

    ./build/mcubridge-gateway/mcubridge-gateway-cpp \
      --cert /etc/mcubridge/gateway.crt \
      --key /etc/mcubridge/gateway.key \
      --client-ca /etc/mcubridge/clients-ca.crt \
      --bind 0.0.0.0 --port 8443

The process rejects clients without a trusted certificate and the required URI
SAN. There is no insecure mode or metadata identity fallback.
