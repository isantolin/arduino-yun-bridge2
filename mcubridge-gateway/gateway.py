#!/usr/bin/env python3
"""Protobuf Cloud Gateway for MCU Bridge v2.

This server acts as the primary cloud endpoint for MPU Daemons.
It terminates connections, decodes CloudEnvelope messages, and processes telemetry/events.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import ssl
import struct
import sys
from pathlib import Path
from typing import Any

# Ensure workspace packages are importable if run directly from workspace
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "mcubridge"))

from mcubridge.protocol import mcubridge_pb2 as pb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcubridge.gateway")


class ProtobufGateway:
    """High-performance direct TCP/TLS Gateway."""

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
        self.server: asyncio.AbstractServer | None = None
        self._connections: dict[str, asyncio.StreamWriter] = {}

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
        self.server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
            ssl=ssl_context,
        )
        addr = self.server.sockets[0].getsockname() if self.server.sockets else (self.host, self.port)
        scheme = "tcps" if ssl_context else "tcp"
        logger.info("Protobuf Cloud Gateway running on %s://%s:%d", scheme, addr[0], addr[1])
        async with self.server:
            await self.server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        device_id = f"anonymous-{peer[0]}:{peer[1]}"

        # If mTLS is used, extract client common name as Device ID
        ssl_obj = writer.get_extra_info("ssl_object")
        if ssl_obj:
            try:
                cert = ssl_obj.getpeercert()
                if cert:
                    for sub in cert.get("subject", []):
                        for key, val in sub:
                            if key == "commonName":
                                device_id = val
            except Exception as e:
                logger.error("Failed to parse client certificate: %s", e)

        logger.info("Device connected: %s", device_id)
        self._connections[device_id] = writer

        try:
            while True:
                # 1. Read 4-byte big-endian length prefix
                len_bytes = await reader.readexactly(4)
                length = struct.unpack(">I", len_bytes)[0]

                # 2. Read full protobuf message body
                data = await reader.readexactly(length)

                # 3. Parse CloudEnvelope
                envelope = pb.CloudEnvelope()
                envelope.ParseFromString(data)

                # 4. Dispatch based on oneof payload
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
                    resp_data = pong.SerializeToString()
                    writer.write(struct.pack(">I", len(resp_data)) + resp_data)
                    await writer.drain()

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

        except (asyncio.IncompleteReadError, OSError):
            logger.info("Device disconnected: %s", device_id)
        finally:
            self._connections.pop(device_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


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
