#!/usr/bin/env python3
"""
[MIL-SPEC/SIL-2] McuBridge Protocol Fuzzer
Mission: Stress test the MCU state machine by injecting protocol-level entropy.
"""

import asyncio
import random
import struct
import argparse
import sys
from binascii import crc32
from cobs import cobs
import serial_asyncio_fast
import structlog
from typing import Final

# Constants from protocol spec
PROTOCOL_VERSION: Final[int] = 0x02
FRAME_DELIMITER: Final[bytes] = b"\x00"

logger = structlog.get_logger("fuzzer")

class ProtocolFuzzer:
    def __init__(self, port: str, baudrate: int):
        self.port = port
        self.baudrate = baudrate
        self.reader = None
        self.writer = None
        self.seq_id = 0

    async def connect(self):
        self.reader, self.writer = await serial_asyncio_fast.open_serial_connection(
            url=self.port, baudrate=self.baudrate
        )
        logger.info("connected", port=self.port, baudrate=self.baudrate)

    def _build_raw_frame(self, cmd: int, seq: int, payload: bytes, override_crc: int = None) -> bytes:
        header = struct.pack(">BHII", PROTOCOL_VERSION, len(payload), cmd, seq)[:7]
        # Re-packing header properly based on frame.py: version(8), len(16), cmd(16), seq(16)
        header = struct.pack(">BHHH", PROTOCOL_VERSION, len(payload), cmd, seq)
        body = header + payload
        crc = override_crc if override_crc is not None else (crc32(body) & 0xFFFFFFFF)
        full = body + struct.pack(">I", crc)
        return cobs.encode(full) + FRAME_DELIMITER

    async def send_raw(self, data: bytes):
        if self.writer:
            self.writer.write(data)
            await self.writer.drain()

    async def fuzz_iteration(self):
        self.seq_id = (self.seq_id + 1) & 0xFFFF
        
        mode = random.choice([
            "valid_ping",
            "invalid_crc",
            "invalid_version",
            "malformed_cobs",
            "oversized_payload",
            "random_garbage",
            "unknown_command"
        ])

        logger.info("fuzz_step", mode=mode, seq=self.seq_id)

        if mode == "valid_ping":
            # CMD_PING is usually 0x0001
            frame = self._build_raw_frame(0x0001, self.seq_id, b"\x01\x02\x03")
            await self.send_raw(frame)
        
        elif mode == "invalid_crc":
            frame = self._build_raw_frame(0x0001, self.seq_id, b"bad_crc", override_crc=0xDEADBEEF)
            await self.send_raw(frame)

        elif mode == "invalid_version":
            # Manual build with version 0xFF
            header = struct.pack(">BHHH", 0xFF, 3, 0x0001, self.seq_id)
            body = header + b"VER"
            crc = crc32(body) & 0xFFFFFFFF
            frame = cobs.encode(body + struct.pack(">I", crc)) + FRAME_DELIMITER
            await self.send_raw(frame)

        elif mode == "malformed_cobs":
            # COBS with 0x00 inside (illegal)
            bad_data = b"\x03\x01\x00\x02" 
            await self.send_raw(bad_data + FRAME_DELIMITER)

        elif mode == "oversized_payload":
            # Length claim 4096 but only 10 bytes sent
            header = struct.pack(">BHHH", PROTOCOL_VERSION, 4096, 0x0001, self.seq_id)
            await self.send_raw(cobs.encode(header + b"SHORT") + FRAME_DELIMITER)

        elif mode == "random_garbage":
            garbage = bytes([random.getrandbits(8) for _ in range(random.randint(1, 32))])
            await self.send_raw(garbage + FRAME_DELIMITER)

        elif mode == "unknown_command":
            # 0x7FFF is usually unassigned
            frame = self._build_raw_frame(0x7FFF, self.seq_id, b"WHOAMI")
            await self.send_raw(frame)

    async def run(self, iterations: int = 100):
        await self.connect()
        
        success_count = 0
        latencies = []
        
        for i in range(iterations):
            # Every 10 iterations, perform a health check
            if i % 10 == 0:
                self.seq_id = (self.seq_id + 1) & 0xFFFF
                ping_frame = self._build_raw_frame(0x0001, self.seq_id, b"PROBE")
                
                start_time = asyncio.get_event_loop().time()
                await self.send_raw(ping_frame)
                
                try:
                    # Wait for any response (MCU usually ACKs or echoes)
                    # For stress, we just wait a bit to see if it responds within 50ms
                    await asyncio.wait_for(self.reader.readuntil(FRAME_DELIMITER), timeout=0.05)
                    latencies.append(asyncio.get_event_loop().time() - start_time)
                    success_count += 1
                except asyncio.TimeoutError:
                    logger.warning("health_probe_timeout", seq=self.seq_id)
            
            await self.fuzz_iteration()
            await asyncio.sleep(0.005) # 100Hz+ stress
            
        if latencies:
            avg_lat = sum(latencies) / len(latencies)
            max_lat = max(latencies)
            logger.info("fuzzing_complete", 
                        iterations=iterations, 
                        health_success_rate=f"{(success_count/(iterations/10 or 1))*100:.1f}%",
                        avg_latency_ms=f"{avg_lat*1000:.2f}",
                        max_latency_ms=f"{max_lat*1000:.2f}")
        else:
            logger.info("fuzzing_complete", iterations=iterations)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--count", type=int, default=1000)
    args = parser.parse_args()

    fuzzer = ProtocolFuzzer(args.port, args.baud)
    try:
        asyncio.run(fuzzer.run(args.count))
    except KeyboardInterrupt:
        pass
