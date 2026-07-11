# Protobuf Cloud Gateway

The Protobuf Cloud Gateway is a high-performance gRPC over HTTP/2 bidirectional streaming server that provides external cloud connectivity.

## Features
- gRPC bidirectional streaming over HTTP/2 (with TLS/mTLS support)
- Uses `grpclib` to process `CloudEnvelope` messages natively in a bidirectional stream
- Low-memory footprint (does not require broker processes)

## Running
Run the gateway locally (e.g. for testing):
```bash
python mcubridge-gateway/gateway.py --no-tls --port 8443
```
