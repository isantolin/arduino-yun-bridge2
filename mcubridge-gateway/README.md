# Protobuf Cloud Gateway

The Protobuf Cloud Gateway is a high-performance gRPC server that provides external cloud connectivity via bidirectional streaming over HTTP/3 (QUIC) with automated fallback to HTTP/2.

## Features
- **gRPC Bidirectional Streaming**: Ultra-low latency asynchronous transport powered by `grpclib`.
- **Deterministic Session Lifecycle FSM**: Connection, mTLS authentication, active streaming, and teardown managed deterministically by `GatewaySessionMachine` (`connected` $\rightarrow$ `authenticated` $\rightarrow$ `active` $\rightarrow$ `closed`).
- **HTTP/3 (QUIC) with HTTP/2 Fallback**: Modern transport supporting 0-RTT connection re-establishment and multiplexed frame streams over UDP/QUIC.
- **TLS 1.3 0-RTT Session Resumption**: Compatible with daemon LMDB session ticket persistence for instantaneous reconnection.
- **mTLS Mutual Authentication**: Strong identity verification using X.509 client and server certificates.
- **Low Memory Footprint**: Pure Python async implementation without external broker daemon dependencies.

## Running

Run the gateway locally:

```bash
# Development (Insecure / Local):
python mcubridge-gateway/gateway.py --no-tls --port 8443

# Production (mTLS over HTTP/3 / HTTP/2):
python mcubridge-gateway/gateway.py \
  --port 8443 \
  --cert /etc/mcubridge/gateway.crt \
  --key /etc/mcubridge/gateway.key \
  --ca /etc/mcubridge/ca.crt
```
