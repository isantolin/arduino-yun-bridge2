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

The upstream pw_transfer::TransferService is registered on the per-peer
upstream pw_rpc::Server with bounded handlers:
- Resource 1: In-memory transfer buffer (64 KiB).
- Resource 2: Bounded atomic file transfer handler (`BoundedFileTransferHandler`)
  enforcing per-principal directories (`<staging-dir>/clients/<id>/` and
  `<staging-dir>/devices/<id>/`), path confinement (no path traversal or
  directory separators), quotas (1 MiB), and atomic temporary file commits on
  `FinalizeWrite`.
- Resource 0x4D454741: staging for the AVR EEPROM image (`eeprom.bin`, at most
  4,080 bytes, the size of the MCU resource; ADR-0007).
No arbitrary host paths are exposed.

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
      --acl /etc/mcubridge/gateway.acl \
      --staging-dir /var/lib/mcubridge-gateway \
      --bind 0.0.0.0 --port 8443

The process rejects clients without a trusted certificate and the required URI
SAN. There is no insecure mode or metadata identity fallback.

### Staging directory (`--staging-dir`)

Transfers are staged under `--staging-dir` (default `/var/lib/mcubridge-gateway`).
At startup the gateway creates it if needed and refuses to run if it is a
symlink or owned by another user; it forces mode 0700 on the root and on its
`clients/` and `devices/` subdirectories. Clients and devices use separate
namespaces, so a client cannot share a directory with a device of the same
name. Staging is single-use: a push moves the client's file to the device and
removes it after the dispatch; a fetch moves the device's file to the client;
each session's directory is deleted when the session closes.

### Client authorization (`--acl`)

`GatewayControl.Dispatch` is deny-by-default ([ADR-0007](../docs/architecture/decisions/0007-cloud-eeprom-access-and-persistent-storage.md)).
Each line of the ACL file grants one client (the `<id>` of its
`spiffe://mcubridge/client/<id>` identity) access to a device and an operation:

    # client     device      operation
    operator-01  device-01   rpc.pb.GpioService/DigitalWrite
    operator-01  *           rpc.pb.SystemService/GetVersion
    provisioner  device-02   *

`*` matches any device or any operation as a whole field; there are no partial
wildcards and the client field cannot be `*`. `#` starts a comment. The file is
read once at startup; an invalid line stops the gateway. Without `--acl` the
gateway starts but denies every dispatch (`event=acl_empty`). A denied request
returns `PERMISSION_DENIED` before the device lookup, so it does not reveal
whether the device is connected. The daemon on each device still applies its
own `cloud_allow_*` permissions.
