#!/usr/bin/env python3
"""
[MIL-SPEC/SIL-2] McuBridge Protocol Fuzzer
Mission: Stress test the MCU state machine by injecting protocol-level entropy.
"""

import asyncio
import random
from binascii import crc32

from cobs import cobs
import serialx
import structlog
import typer
from collections.abc import Callable
import secrets
from typing import Annotated

from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame
from mcubridge.protocol import mcubridge_pb2 as pb

logger = structlog.get_logger("fuzzer")


class ProtocolFuzzer:
    def __init__(self, port: str, baudrate: int) -> None:
        self.port = port
        self.baudrate = baudrate
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.seq_id = 0

    async def connect(self) -> None:
        self.reader, self.writer = await serialx.open_serial_connection(url=self.port, baudrate=self.baudrate)
        logger.info("connected", port=self.port, baudrate=self.baudrate)

    def _build_raw_frame(self, cmd: int, seq: int, payload: bytes) -> bytes:
        return cobs.encode(build_frame(command_id=cmd, sequence_id=seq, payload=payload)) + protocol.FRAME_DELIMITER

    def _build_envelope_frame(
        self,
        command_id: int,
        payload: bytes,
        *,
        version: int = protocol.PROTOCOL_VERSION,
        override_crc: int | None = None,
    ) -> bytes:
        envelope = pb.RpcEnvelope(
            version=version,
            command_id=command_id,
            sequence_id=self.seq_id,
            encrypted_payload_with_tag=payload,
        )
        body = envelope.SerializeToString()
        crc_val = override_crc if override_crc is not None else (crc32(body) & protocol.CRC32_MASK)
        raw_frame = body + (crc_val & protocol.CRC32_MASK).to_bytes(protocol.CRC_SIZE, "little")
        return cobs.encode(raw_frame) + protocol.FRAME_DELIMITER

    async def send_raw(self, data: bytes) -> None:
        if self.writer:
            self.writer.write(data)
            await self.writer.drain()

    def _get_fuzz_generators(self) -> dict[str, Callable[[], bytes]]:
        return {
            "valid_ping": lambda: self._build_raw_frame(0x0001, self.seq_id, b"\x01\x02\x03"),
            "invalid_crc": lambda: self._build_envelope_frame(
                0x0001, b"bad_crc", override_crc=protocol.BOOTLOADER_MAGIC
            ),
            "invalid_version": lambda: self._build_envelope_frame(0x0001, b"VER", version=protocol.UINT8_MASK),
            "malformed_cobs": lambda: b"\x03\x01\x00\x02" + protocol.FRAME_DELIMITER,
            "oversized_payload": lambda: self._build_envelope_frame(0x0001, b"A" * 300),
            "random_garbage": lambda: secrets.token_bytes(random.randint(1, 32)) + protocol.FRAME_DELIMITER,
            "unknown_command": lambda: self._build_raw_frame(0x7FFF, self.seq_id, b"WHOAMI"),
        }

    async def fuzz_iteration(self) -> None:
        self.seq_id = (self.seq_id + 1) & protocol.UINT16_MAX
        generators = self._get_fuzz_generators()
        mode = random.choice(list(generators))
        logger.info("fuzz_step", mode=mode, seq=self.seq_id)
        await self.send_raw(generators[mode]())

    async def run(self, iterations: int = 100) -> None:
        await self.connect()

        success_count = 0
        latencies: list[float] = []

        for i in range(iterations):
            if i % 10 == 0:
                self.seq_id = (self.seq_id + 1) & protocol.UINT16_MAX
                ping_frame = self._build_raw_frame(0x0001, self.seq_id, b"PROBE")

                start_time = asyncio.get_event_loop().time()
                await self.send_raw(ping_frame)

                try:
                    if self.reader:
                        await asyncio.wait_for(self.reader.readuntil(protocol.FRAME_DELIMITER), timeout=0.05)
                        latencies.append(asyncio.get_event_loop().time() - start_time)
                        success_count += 1
                except (TimeoutError, asyncio.IncompleteReadError):
                    logger.warning("health_probe_timeout", seq=self.seq_id)

            await self.fuzz_iteration()
            await asyncio.sleep(0.005)

        if latencies:
            avg_lat = sum(latencies) / len(latencies)
            max_lat = max(latencies)
            logger.info(
                "fuzzing_complete",
                iterations=iterations,
                health_success_rate=f"{(success_count / (iterations / 10 or 1)) * 100:.1f}%",
                avg_latency_ms=f"{avg_lat * 1000:.2f}",
                max_latency_ms=f"{max_lat * 1000:.2f}",
            )
        else:
            logger.info("fuzzing_complete", iterations=iterations)


cli = typer.Typer(help="[MIL-SPEC/SIL-2] McuBridge Protocol Fuzzer", add_completion=False)


@cli.command()
def main(
    port: Annotated[str, typer.Option("--port", help="Serial port device")] = "/dev/ttyUSB0",
    baud: Annotated[int, typer.Option("--baud", help="Serial baudrate")] = protocol.DEFAULT_BAUDRATE,
    count: Annotated[int, typer.Option("--count", help="Number of fuzz iterations")] = 1000,
) -> None:
    fuzzer = ProtocolFuzzer(port, baud)
    try:
        asyncio.run(fuzzer.run(count))
    except KeyboardInterrupt:
        print("\n[INFO] Fuzzer interrupted by user.")


if __name__ == "__main__":
    cli()
