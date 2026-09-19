# MCU Bridge Protobuf Cloud Gateway (Dedicated Central Server)

The Protobuf Cloud Gateway is a high-performance, enterprise-grade gRPC hub designed to run on a **dedicated central server or cloud instance** (Linux x86_64 / ARM64, VM, or Docker container). It acts as the central command, telemetry ingestion, and observability plane for $N$ distributed Arduino Yún (McuBridge) edge devices.

## Features

- **Hub-and-Spoke (N:1) Architecture**: Multiplexes hundreds or thousands of remote Arduino edge nodes over outbound-only connections, completely bypassing edge NAT and firewall restrictions.
- **gRPC Bidirectional Streaming**: Ultra-low latency asynchronous transport powered by `grpclib` and `uvloop`.
- **Deterministic Session Lifecycle FSM**: Connection, mTLS authentication, active streaming, and teardown managed deterministically by `GatewaySessionMachine` (`connected` $\rightarrow$ `authenticated` $\rightarrow$ `active` $\rightarrow$ `closed`).
- **Fleet-Wide Prometheus Observability**: Exposes a centralized `/metrics` HTTP endpoint aggregating connection states, queue depths, link synchronization, and drop counts across all active devices.
- **TSDB Time-Series Ingestion**: Native adapter supporting InfluxDB / VictoriaMetrics Line Protocol for zero-overhead streaming of telemetry to industrial databases.
- **Asynchronous Command Orchestration**: Northbound dispatch mechanism (`send_command`) with monotonic sequence tracking and asynchronous response correlation.
- **mTLS Mutual Authentication**: Cryptographically verified device identification using X.509 client certificate Common Name extraction (`extract_peer_identity`).
- **HTTP/3 (QUIC) with HTTP/2 Fallback**: Modern transport supporting 0-RTT connection re-establishment over UDP/QUIC.

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
python mcubridge-gateway/gateway.py --no-tls --port 8443 --metrics-port 9100

# Production (mTLS over HTTP/3 / HTTP/2 with Prometheus and TSDB):
python mcubridge-gateway/gateway.py \
  --host 0.0.0.0 \
  --port 8443 \
  --metrics-port 9100 \
  --tsdb-url http://victoriametrics:8428/write \
  --cert /etc/mcubridge/gateway.crt \
  --key /etc/mcubridge/gateway.key \
  --ca /etc/mcubridge/ca.crt
```
