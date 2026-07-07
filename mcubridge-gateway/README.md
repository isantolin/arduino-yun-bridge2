# Protobuf Cloud Gateway

The Protobuf Cloud Gateway is a high-performance direct TCP/TLS server that replaces Mosquitto and standard MQTT brokers for external cloud connectivity.

## Features
- Direct TCP or secure TLS connections
- Decodes `CloudEnvelope` messages natively
- Low-memory footprint (does not require broker processes)

## Running
Run the gateway locally (e.g. for testing):
```bash
python mcubridge-gateway/gateway.py --no-tls --port 8443
```
