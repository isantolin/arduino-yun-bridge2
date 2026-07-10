#!/usr/bin/env python3
"""Protobuf Cloud Gateway for MCU Bridge v2.

This server acts as the primary cloud endpoint for MPU Daemons, running as a gRPC server.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import ssl
import sys
from pathlib import Path

from typing import TYPE_CHECKING

from grpclib.server import Server, Stream

if TYPE_CHECKING:
    from mcubridge.protocol import mcubridge_pb2 as pb
    from mcubridge.protocol.mcubridge_grpc import CloudBridgeBase
else:
    # Ensure workspace packages are importable if run directly from workspace
    REPO_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "mcubridge"))

    from mcubridge.protocol import mcubridge_pb2 as pb
    from mcubridge.protocol.mcubridge_grpc import CloudBridgeBase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcubridge.gateway")


class CloudBridgeService(CloudBridgeBase):
    def __init__(self, gateway: ProtobufGateway) -> None:
        self.gateway = gateway

    async def Session(self, stream: Stream[pb.CloudEnvelope, pb.CloudEnvelope]) -> None:
        peer = stream.peer.addr()
        device_id = f"anonymous-{peer[0]}:{peer[1]}" if peer else "anonymous-unknown"

        cert = stream.peer.cert()
        if cert:
            try:
                for sub in cert.get("subject", []):
                    for key, val in sub:
                        if key == "commonName":
                            device_id = val
            except (ssl.SSLError, AttributeError, KeyError, TypeError) as e:
                logger.error("Failed to parse client certificate: %s", e)
                return

        logger.info("Device connected: %s", device_id)
        self.gateway.connections[device_id] = stream

        try:
            async for envelope in stream:
                payload_type = envelope.WhichOneof("payload")
                logger.debug(
                    "Received envelope from %s (seq=%d, payload=%s)", device_id, envelope.sequence_id, payload_type
                )

                if payload_type == "ping":
                    # Respond with KeepalivePong
                    pong = pb.CloudEnvelope(
                        protocol_version=2,
                        device_id="CLOUD_GW",
                        sequence_id=envelope.sequence_id,
                        pong=pb.KeepalivePong(roundtrip_ms=0),
                    )
                    await stream.send_message(pong)

                elif payload_type == "telemetry":
                    logger.info("Processed telemetry from %s", device_id)

                elif payload_type == "event":
                    evt = envelope.event
                    logger.warning("Event from %s: [%s] %s", device_id, evt.event_type, evt.description)

                elif payload_type == "command_response":
                    logger.info(
                        "Received command response from %s (status=%d)",
                        device_id,
                        envelope.command_response.status_code,
                    )
        except (asyncio.CancelledError, OSError):
            pass
        finally:
            logger.info("Device disconnected: %s", device_id)
            self.gateway.connections.pop(device_id, None)


class ProtobufGateway:
    """High-performance gRPC Gateway."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8443,
        use_tls: bool = True,
        cert_file: str | None = None,
        key_file: str | None = None,
        ca_file: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.cert_file = cert_file
        self.key_file = key_file
        self.ca_file = ca_file
        self.server: Server | None = None
        self.connections: dict[str, Stream[pb.CloudEnvelope, pb.CloudEnvelope]] = {}

    def _get_ssl_context(self) -> ssl.SSLContext | None:
        if not self.use_tls:
            return None

        if not self.cert_file or not self.key_file:
            logger.warning("TLS enabled but certificate/key files not provided. Running without TLS.")
            return None

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        if self.ca_file:
            context.load_verify_locations(cafile=self.ca_file)
            context.verify_mode = ssl.CERT_REQUIRED
            logger.info("Mutual TLS (mTLS) client verification enabled.")
        else:
            context.verify_mode = ssl.CERT_NONE
            logger.info("TLS enabled (server-only authentication).")
        return context

    async def run(self) -> None:
        ssl_context = self._get_ssl_context()
        self.server = Server([CloudBridgeService(self)])
        await self.server.start(self.host, self.port, ssl=ssl_context)

        scheme = "tcps" if ssl_context else "tcp"
        logger.info("gRPC Cloud Gateway running on %s://%s:%d", scheme, self.host, self.port)
        await self.server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description="MCU Bridge Protobuf Gateway")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8443, help="Port to listen on")
    parser.add_argument("--no-tls", action="store_true", help="Disable TLS (insecure mode)")
    parser.add_argument("--cert", help="Path to server SSL certificate file")
    parser.add_argument("--key", help="Path to server SSL private key file")
    parser.add_argument("--ca", help="Path to CA file for client certificate verification")
    args = parser.parse_args()

    gateway = ProtobufGateway(
        host=args.host,
        port=args.port,
        use_tls=not args.no_tls,
        cert_file=args.cert,
        key_file=args.key,
        ca_file=args.ca,
    )

    try:
        asyncio.run(gateway.run())
    except KeyboardInterrupt:
        logger.info("Gateway terminated by user.")


if __name__ == "__main__":
    main()
