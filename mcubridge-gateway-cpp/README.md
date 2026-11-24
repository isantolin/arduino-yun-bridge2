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
- Only client-role sessions register the northbound `GatewayControl` and upstream
  `pw.metric.proto.MetricService` (`pw_metric`) services. `GatewayControl.Health`
  reports readiness and active session counts, while `MetricService.Get` / `Walk`
  export tokenized runtime counters (`gateway` group) decodable from the binary's
  `.pw_tokenizer.entries` ELF section.
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

## Build and Install

Pigweed source is the recursively initialized submodule pinned at
`third_party/pigweed`; it is not downloaded at an independent revision.

On Debian/Ubuntu host:

    sudo apt-get install -y build-essential cmake ninja-build libssl-dev protobuf-compiler python3 python3-venv
    python3 -m venv .venv-gateway
    . .venv-gateway/bin/activate
    pip install "pigweed==0.0.24" "protobuf==5.28.3"
    cmake -S mcubridge-gateway-cpp -B build/mcubridge-gateway -G Ninja -DCMAKE_BUILD_TYPE=Release
    cmake --build build/mcubridge-gateway
    sudo cmake --install build/mcubridge-gateway --prefix /usr/local

`cmake --install` installs:
- `<prefix>/bin/mcubridge-gateway-cpp`
- `<prefix>/share/mcubridge-gateway/mcubridge-gateway.service`
- `<prefix>/share/mcubridge-gateway/mcubridge-gateway.env.example`

The project explicitly selects POSIX/STL Pigweed backends. It does not reuse
the AVR CMake toolchain or the AVR cooperative transfer-thread backend.

## Run

    mcubridge-gateway-cpp --help
    mcubridge-gateway-cpp \
      --cert /etc/mcubridge-gateway/gateway.crt \
      --key /etc/mcubridge-gateway/gateway.key \
      --client-ca /etc/mcubridge-gateway/clients-ca.crt \
      --acl /etc/mcubridge-gateway/gateway.acl \
      --staging-dir /var/lib/mcubridge-gateway \
      --bind * --port 8443

The process rejects clients without a trusted certificate and the required URI
SAN. There is no insecure mode or metadata identity fallback.

## Production Deployment

### Hardened OCI / Docker Container (`Dockerfile`)

Build from the **repository root** (required because CMake compiles pinned
sources from `third_party/pigweed` and schemas from `tools/protocol`;
`mcubridge-gateway-cpp/Dockerfile.dockerignore` scopes the BuildKit context
without affecting `build-apk.yml`'s `Dockerfile.apk-feed`):

    docker build -f mcubridge-gateway-cpp/Dockerfile -t mcubridge-gateway:latest .

The multi-stage `debian:bookworm-slim` image compiles the Release binary in the
`builder` stage and copies only the stripped executable onto a minimal `libssl3`
runtime image running as unprivileged user `mcubridge:mcubridge` (UID/GID
`10001`), exposing `8443/tcp`, handling `SIGTERM`, and verifying the listening
socket via `/proc/net/tcp{,6}` in `HEALTHCHECK`.

Run with read-only credentials/ACL and a private `0700` staging directory owned
by UID/GID `10001`:

    docker run -d --name mcubridge-gateway \
      --read-only \
      --cap-drop=ALL \
      --security-opt=no-new-privileges:true \
      --tmpfs /tmp:noexec,nosuid,size=16m \
      -p 8443:8443 \
      -v /etc/mcubridge-gateway:/etc/mcubridge-gateway:ro \
      -v /var/lib/mcubridge-gateway:/var/lib/mcubridge-gateway:rw \
      mcubridge-gateway:latest

### Hardened Linux `systemd` Service

Install the service account, configuration, TLS material, and sandboxed unit:

    sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin mcubridge
    sudo install -d -o root -g mcubridge -m 0750 /etc/mcubridge-gateway
    sudo install -o root -g mcubridge -m 0640 \
      /usr/local/share/mcubridge-gateway/mcubridge-gateway.env.example \
      /etc/mcubridge-gateway/env
    sudo install -o root -g root -m 0644 \
      /usr/local/share/mcubridge-gateway/mcubridge-gateway.service \
      /etc/systemd/system/mcubridge-gateway.service
    # Place gateway.crt (0644), gateway.key (0640 root:mcubridge or 0600 mcubridge),
    # clients-ca.crt (0644), and gateway.acl (0640 root:mcubridge) in /etc/mcubridge-gateway/.
    sudo systemctl daemon-reload
    sudo systemctl enable --now mcubridge-gateway.service

The unit runs with `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
`NoNewPrivileges=yes`, empty `CapabilityBoundingSet=`,
`RestrictAddressFamilies=AF_INET AF_INET6`, `MemoryDenyWriteExecute=yes`,
`ReadOnlyPaths=/etc/mcubridge-gateway`, and `StateDirectory=mcubridge-gateway`
(`0700`). To bind a privileged port `< 1024`, add a `systemctl edit` drop-in
granting `CapabilityBoundingSet=CAP_NET_BIND_SERVICE` and
`AmbientCapabilities=CAP_NET_BIND_SERVICE`.

### OpenWrt APK Packaging Boundary

`mcubridge-gateway-cpp` is intentionally **not** packaged as an OpenWrt router
APK (`.github/workflows/build-apk.yml` and `feeds/`). OpenWrt APKs target edge
routers running the `mcubridge` daemon and `luci-app-mcubridge`, whereas
`mcubridge-gateway-cpp` is the upstream cloud/fleet collector server that
terminates up to 256 concurrent TLS 1.3 mTLS sessions and per-session file
staging on a Linux host or container orchestrator. Its supported production
distribution formats are `cmake --install`, the multi-stage OCI `Dockerfile`,
and the hardened `systemd` unit above.

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
