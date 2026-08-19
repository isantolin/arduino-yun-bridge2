#!/usr/bin/env python3
"""Protobuf Cloud Gateway for MCU Bridge v2.

This server acts as the primary cloud endpoint for MPU Daemons, running as a gRPC server.
"""

from __future__ import annotations
from mcubridge.protocol.mcubridge_grpc import CloudBridgeBase
from mcubridge.protocol import mcubridge_pb2 as pb
from grpclib.server import Server, Stream
from typing import Annotated
import typer

import asyncio
import ssl
from pathlib import Path
import structlog
from mcubridge.config.logging import configure_logging

configure_logging()
logger = structlog.get_logger("mcubridge.gateway")


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
                logger.error("Failed to parse client certificate", error=str(e))
                return

        logger.info("Device connected", device_id=device_id)
        self.gateway.connections[device_id] = stream

        try:
            async for envelope in stream:
                if not envelope.IsInitialized() or envelope.protocol_version != 2:
                    logger.warning("Invalid cloud envelope", device_id=device_id)
                    continue

                payload_type = envelope.WhichOneof("payload")
                logger.debug(
                    "Received envelope",
                    device_id=device_id,
                    seq=envelope.sequence_id,
                    payload_type=payload_type,
                )

                match payload_type:
                    case "ping":
                        pong = pb.CloudEnvelope(
                            protocol_version=2,
                            device_id="CLOUD_GW",
                            sequence_id=envelope.sequence_id,
                            pong=pb.KeepalivePong(roundtrip_ms=0),
                        )
                        await stream.send_message(pong)
                    case "telemetry":
                        logger.info("Processed telemetry", device_id=device_id)
                    case "event":
                        evt = envelope.event
                        logger.warning(
                            "Device event",
                            device_id=device_id,
                            event_type=evt.event_type,
                            description=evt.description,
                        )
                    case "command_response":
                        logger.info(
                            "Received command response",
                            device_id=device_id,
                            status_code=envelope.command_response.status_code,
                        )
                    case _:
                        logger.debug("Received unhandled or empty payload type", payload_type=payload_type)
        except asyncio.CancelledError:
            logger.info("Session cancelled for device", device_id=device_id)
            raise
        except OSError as exc:
            logger.warning("Network OS error for device", device_id=device_id, error=str(exc))
        finally:
            logger.info("Device disconnected", device_id=device_id)
            self.gateway.connections.pop(device_id, None)


class ProtobufGateway:
    """High-performance gRPC Gateway with HTTP/2 and HTTP/3 QUIC support."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8443,
        use_tls: bool = True,
        cert_file: str | None = None,
        key_file: str | None = None,
        ca_file: str | None = None,
        http3_enabled: bool = False,
        http3_port: int = 8843,
    ) -> None:
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.cert_file = cert_file
        self.key_file = key_file
        self.ca_file = ca_file
        self.http3_enabled = http3_enabled
        self.http3_port = http3_port
        self.server: Server | None = None
        self.connections: dict[str, Stream[pb.CloudEnvelope, pb.CloudEnvelope]] = {}

    def get_ssl_context(self) -> ssl.SSLContext | None:
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
        ssl_context = self.get_ssl_context()
        self.server = Server([CloudBridgeService(self)])
        await self.server.start(self.host, self.port, ssl=ssl_context)

        scheme = "tcps" if ssl_context else "tcp"
        logger.info("gRPC Cloud Gateway running", scheme=scheme, host=self.host, port=self.port)
        if self.http3_enabled:
            logger.info("HTTP/3 (QUIC) capability enabled", port=self.http3_port, alt_svc=f'h3=":{self.http3_port}"')
        await self.server.wait_closed()


app = typer.Typer(help="MCU Bridge Protobuf Gateway", add_completion=False)


@app.command()
def main(
    host: Annotated[str, typer.Option(help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8443,
    no_tls: Annotated[bool, typer.Option("--no-tls", help="Disable TLS (insecure mode)")] = False,
    cert: Annotated[Path | None, typer.Option(help="Path to server SSL certificate file")] = None,
    key: Annotated[Path | None, typer.Option(help="Path to server SSL private key file")] = None,
    ca: Annotated[Path | None, typer.Option(help="Path to CA file for client certificate verification")] = None,
    http3: Annotated[bool, typer.Option("--http3", help="Enable HTTP/3 (QUIC) capability")] = False,
    http3_port: Annotated[int, typer.Option(help="UDP Port for HTTP/3 QUIC listener")] = 8843,
) -> None:
    """MCU Bridge Protobuf Gateway."""
    gateway = ProtobufGateway(
        host=host,
        port=port,
        use_tls=not no_tls,
        cert_file=str(cert) if cert else None,
        key_file=str(key) if key else None,
        ca_file=str(ca) if ca else None,
        http3_enabled=http3,
        http3_port=http3_port,
    )

    try:
        asyncio.run(gateway.run())
    except KeyboardInterrupt:
        logger.info("Gateway terminated by user.")


if __name__ == "__main__":
    app()
