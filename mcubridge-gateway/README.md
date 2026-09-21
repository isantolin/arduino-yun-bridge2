# MCU Bridge Protobuf Cloud Gateway (Dedicated Central Server)

The Protobuf Cloud Gateway is a high-performance, enterprise-grade gRPC hub designed to run on a **dedicated central server or cloud instance** (Linux x86_64 / ARM64, VM, or Docker container). It acts as the central command, telemetry ingestion, Northbound RPC routing, and observability plane for $N$ distributed Arduino Yún (McuBridge) edge devices.

## Key Capabilities

- **Hub-and-Spoke (N:1) Architecture**: Multiplexes hundreds or thousands of remote Arduino edge nodes over outbound-only connections, completely bypassing edge NAT and firewall restrictions.
- **Northbound LocalBridge RPC Hub (`GatewayLocalBridgeService`)**: Implements all 22 typed `LocalBridge` RPC methods plus real-time console streaming (`SubscribeConsole`), routing calls seamlessly to the target edge device over active session streams.
- **Strictly Explicit Device Resolution (`x-device-id`)**: All client RPC invocations must specify the target device ID via gRPC metadata (`x-device-id`). Unspecified device IDs fail with `INVALID_ARGUMENT`, and disconnected devices fail with `UNAVAILABLE`. No guessing, no defaults, and no legacy fallbacks.
- **gRPC Bidirectional Streaming**: Ultra-low latency asynchronous transport powered by `grpclib` and `uvloop`.
- **Deterministic Session Lifecycle FSM**: Connection, mTLS authentication, active streaming, and teardown managed deterministically by `GatewaySessionMachine` (`connected` $\rightarrow$ `authenticated` $\rightarrow$ `active` $\rightarrow$ `closed`).
- **Fleet-Wide Prometheus Observability**: Exposes a centralized `/metrics` HTTP endpoint (default port `9100`) aggregating connection states, queue depths, link synchronization, and drop counts across all active devices (`FleetMetrics`).
- **TSDB Time-Series Ingestion**: Native adapter supporting InfluxDB / VictoriaMetrics Line Protocol for zero-overhead streaming of telemetry to industrial databases.
- **mTLS Mutual Authentication**: Cryptographically verified device identification using X.509 client certificate Common Name extraction (`extract_peer_identity`), with fallback to `x-device-id` metadata in insecure/development mode (`--no-tls`).
- **HTTP/3 (QUIC) with HTTP/2 Fallback**: Modern transport supporting 0-RTT connection re-establishment over UDP/QUIC.

---

## Gateway Architecture & Northbound Flow

```
┌────────────────────────────────────────────────────────┐
│  Client Applications / Tools / Dashboards              │
│  (mcubridge-client-examples, automation scripts, CLI)  │
└───────────────────────────┬────────────────────────────┘
                            │ gRPC LocalBridge RPCs
                            │ Metadata: x-device-id: <device_id>
                            ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                    mcubridge-gateway (Central Server)                     │
│                                                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐  │
│  │ GatewayLocalBridgeService (LocalBridgeBase)                         │  │
│  │  - Validates and extracts x-device-id from stream metadata          │  │
│  │  - Resolves active DeviceSession in active_sessions registry        │  │
│  │  - Dispatches CommandRequest(command_path="rpc/<method>") to device │  │
│  │  - Multiplexes SubscribeConsole queues to client streams            │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
│                                                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐  │
│  │ ProtobufGateway (BridgeCloudGatewayBase)                            │  │
│  │  - Accepts outbound Session streams from edge devices               │  │
│  │  - Manages GatewaySessionMachine lifecycle & mTLS auth              │  │
│  │  - Ingests telemetry, updates Prometheus metrics, forwards to TSDB  │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
└───────────────────────────┬───────────────────────────────────────────────┘
                            │ Outbound Bidirectional gRPC Stream
                            │ CommandRequest / CommandResponse
                            ▼
┌────────────────────────────────────────────────────────┐
│             McuBridge Daemon (Edge Linux / MPU)        │
│  - Executes local_bridge_service.execute_rpc()         │
│  - Pure Telemetry Push                                 │
└───────────────────────────┬────────────────────────────┘
                            │ Serial Link (COBS/R + CRC32)
                            ▼
┌────────────────────────────────────────────────────────┐
│                 MCU Firmware (Arduino AVR)             │
└────────────────────────────────────────────────────────┘
```

---

## Supported `LocalBridge` RPC Methods

`GatewayLocalBridgeService` exposes all 22 unary operations and the real-time streaming console endpoint defined in `tools/protocol/mcubridge.proto`:

| Category | RPC Method | Request Message | Response Message | Description |
|---|---|---|---|---|
| **GPIO & Pins** | `SetPinMode` | `PinMode` | `GenericResponse` | Configures pin direction (INPUT, OUTPUT, INPUT_PULLUP). |
| | `DigitalWrite` | `DigitalWrite` | `GenericResponse` | Sets digital output level (HIGH, LOW). |
| | `DigitalRead` | `PinRead` | `DigitalReadResponse` | Reads immediate digital pin level. |
| | `AnalogWrite` | `AnalogWrite` | `GenericResponse` | Sets PWM output duty cycle. |
| | `AnalogRead` | `PinRead` | `AnalogReadResponse` | Reads 10-bit ADC value from analog input. |
| | `PinSubscribe` | `PinSubscribeRequest` | `PinSubscribeResponse` | Subscribes to pin state change notifications. |
| **Datastore** | `DatastorePut` | `DatastorePut` | `GenericResponse` | Stores a key-value pair in RAM datastore. |
| | `DatastoreGet` | `DatastoreGet` | `DatastoreGetResponse` | Retrieves a value for a key from RAM datastore. |
| **Mailbox** | `MailboxPush` | `MailboxPush` | `GenericResponse` | Pushes a message into the MCU mailbox FIFO queue. |
| | `MailboxRead` | `SubscribeRequest` | `MailboxReadResponse` | Reads messages from the MCU mailbox queue. |
| **Filesystem** | `FileWrite` | `FileWrite` | `GenericResponse` | Writes data to a file on the storage partition. |
| | `FileRead` | `FileRead` | `FileReadResponse` | Reads content from a stored file. |
| | `FileRemove` | `FileRemove` | `GenericResponse` | Deletes a file from the storage partition. |
| **Processes** | `ProcessRunAsync` | `ProcessRunAsync` | `ProcessRunAsyncResponse` | Spawns an asynchronous sub-process on the MPU. |
| | `ProcessPoll` | `ProcessPoll` | `ProcessPollResponse` | Polls execution status and stdout/stderr of a process. |
| | `ProcessKill` | `ProcessKill` | `GenericResponse` | Terminates a running process via PID. |
| **SPI Bus** | `SpiTransfer` | `SpiTransfer` | `SpiTransferResponse` | Executes full-duplex SPI master byte transfers. |
| | `SpiConfigure` | `SpiConfig` | `GenericResponse` | Configures SPI clock, bit order, and data mode. |
| **System Info** | `GetVersion` | `SubscribeRequest` | `VersionResponse` | Retrieves protocol and firmware versions. |
| | `GetFreeMemory` | `SubscribeRequest` | `FreeMemoryResponse` | Retrieves available SRAM on the MCU. |
| | `GetStatus` | `SubscribeRequest` | `BridgeStatus` | Retrieves synchronization and link status snapshot. |
| **Generic** | `Publish` | `CloudQueuedPublish` | `CloudQueuedPublish` | Direct raw publish / envelope dispatch. |
| **Streaming** | `SubscribeConsole` | `SubscribeRequest` | `stream CloudQueuedPublish` | Real-time multiplexed stream of MCU console output. |

---

## Device Resolution & Error Handling

When an RPC arrives at `GatewayLocalBridgeService`:

1. **Extraction:** The gateway inspects stream metadata for the key `x-device-id`.
2. **Missing Metadata (`INVALID_ARGUMENT`):** If no `x-device-id` header is present, the gateway aborts with `GRPCError(Status.INVALID_ARGUMENT, "Missing x-device-id in request metadata")`.
3. **Disconnected Device (`UNAVAILABLE`):** If the requested device is not registered in the gateway's active session registry, it aborts with `GRPCError(Status.UNAVAILABLE, "Device '<id>' is not connected to gateway")`.
4. **Execution & Correlation:** If connected, the payload is forwarded through `DeviceSession.send_command(...)` with an auto-incrementing monotonic sequence number (`seq`). The response from the edge daemon is asynchronously correlated and returned to the client.

---

## Deployment Options

### 1. Docker & Docker Compose (Recommended for Production)

Run the gateway in a lightweight container:

```bash
docker compose -f mcubridge-gateway/docker-compose.yml up -d
```

### 2. Standalone Systemd Service (Linux Bare-Metal / VM)

Install the systemd unit file on Rocky Linux, RHEL, Ubuntu, or Debian:

```bash
sudo cp mcubridge-gateway/mcubridge-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mcubridge-gateway
```

### 3. Direct Execution via CLI

Run the gateway directly with Python 3.13+:

```bash
# Development (Insecure / Local):
python3 mcubridge-gateway/gateway.py --no-tls --port 8443 --metrics-port 9100

# Production (mTLS over HTTP/3 / HTTP/2 with Prometheus and TSDB):
python3 mcubridge-gateway/gateway.py \
  --host 0.0.0.0 \
  --port 8443 \
  --metrics-port 9100 \
  --tsdb-url http://victoriametrics:8428/write \
  --cert /etc/mcubridge/gateway.crt \
  --key /etc/mcubridge/gateway.key \
  --ca /etc/mcubridge/ca.crt
```
