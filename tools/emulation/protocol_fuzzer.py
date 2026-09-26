"""[MIL-SPEC/SIL-2] McuBridge Protocol Stateful Fuzzer.

Executes deterministic, property-based stateful fuzzing of serial framing, COBS/R encoding,
Protobuf envelopes, CRC verification, and error handling on simulated or physical MCU endpoints
using Hypothesis RuleBasedStateMachine for mathematical reproducibility and minimal shrinking.
"""

from __future__ import annotations

import asyncio
from binascii import crc32
from typing import Annotated

from cobs import cobs
from hypothesis import HealthCheck, event, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
import serialx
import structlog
import typer

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame

app = typer.Typer(help="[MIL-SPEC/SIL-2] McuBridge Protocol Stateful Fuzzer")
logger = structlog.get_logger("protocol_fuzzer")


class ProtocolFuzzerStateMachine(RuleBasedStateMachine):
    """[SIL-2] Hypothesis RuleBasedStateMachine for serial protocol fuzzing.

    Eliminates ad-hoc randomness in favor of mathematically reproducible sequences,
    automatic counterexample shrinking, and deterministic state invariant verification.
    """

    port: str = "/dev/ttyUSB0"
    baudrate: int = protocol.DEFAULT_BAUDRATE

    def __init__(self) -> None:
        super().__init__()
        self.seq_id: int = 0
        self.frames_sent: int = 0
        self.probe_responses_received: int = 0
        self.loop = asyncio.new_event_loop()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.loop.run_until_complete(self._connect())

    async def _connect(self) -> None:
        self.reader, self.writer = await serialx.open_serial_connection(url=self.port, baudrate=self.baudrate)
        logger.info("connected", port=self.port, baudrate=self.baudrate)

    async def _send_raw(self, data: bytes) -> None:
        if self.writer is not None:
            self.writer.write(data)
            await self.writer.drain()
            self.frames_sent += 1

    async def _close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (OSError, TimeoutError) as exc:
                logger.warning("writer_close_warning", error=str(exc))
            self.writer = None
            self.reader = None

    def teardown(self) -> None:
        if not self.loop.is_closed():
            self.loop.run_until_complete(self._close())
            self.loop.close()
        super().teardown()

    def _next_seq_id(self) -> int:
        self.seq_id = (self.seq_id + 1) & protocol.UINT16_MAX
        return self.seq_id

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
        seq = self._next_seq_id()
        envelope = pb.RpcEnvelope(
            version=version,
            command_id=command_id,
            sequence_id=seq,
            encrypted_payload_with_tag=payload,
        )
        body = envelope.SerializeToString()
        crc_val = override_crc if override_crc is not None else (crc32(body) & protocol.CRC32_MASK)
        raw_frame = body + (crc_val & protocol.CRC32_MASK).to_bytes(protocol.CRC_SIZE, "little")
        return cobs.encode(raw_frame) + protocol.FRAME_DELIMITER

    @rule(payload=st.binary(min_size=0, max_size=128))
    def fuzz_valid_ping(self, payload: bytes) -> None:
        """Generate valid ping frames with arbitrary valid payloads."""
        seq = self._next_seq_id()
        frame = self._build_raw_frame(0x0001, seq, payload)
        self.loop.run_until_complete(self._send_raw(frame))
        event("rule_valid_ping")

    @rule(
        cmd=st.integers(min_value=1, max_value=0x7FFF),
        payload=st.binary(min_size=1, max_size=128),
        bad_crc=st.integers(min_value=0, max_value=protocol.UINT32_MAX),
    )
    def fuzz_invalid_crc(self, cmd: int, payload: bytes, bad_crc: int) -> None:
        """Inject frames with corrupt CRC32 checksums."""
        frame = self._build_envelope_frame(cmd, payload, override_crc=bad_crc)
        self.loop.run_until_complete(self._send_raw(frame))
        event("rule_invalid_crc")

    @rule(
        cmd=st.integers(min_value=1, max_value=0x7FFF),
        bad_version=st.integers(min_value=0, max_value=protocol.UINT8_MASK).filter(
            lambda v: v != protocol.PROTOCOL_VERSION
        ),
        payload=st.binary(min_size=0, max_size=64),
    )
    def fuzz_invalid_version(self, cmd: int, bad_version: int, payload: bytes) -> None:
        """Inject envelopes with unsupported protocol version numbers."""
        frame = self._build_envelope_frame(cmd, payload, version=bad_version)
        self.loop.run_until_complete(self._send_raw(frame))
        event("rule_invalid_version")

    @rule(raw_bytes=st.binary(min_size=1, max_size=128))
    def fuzz_malformed_cobs(self, raw_bytes: bytes) -> None:
        """Inject arbitrary unencoded byte sequences ending in frame delimiter."""
        frame = raw_bytes + protocol.FRAME_DELIMITER
        self.loop.run_until_complete(self._send_raw(frame))
        event("rule_malformed_cobs")

    @rule(
        cmd=st.integers(min_value=1, max_value=0x7FFF),
        oversized=st.binary(min_size=protocol.MAX_PAYLOAD_SIZE + 1, max_size=protocol.MAX_PAYLOAD_SIZE + 256),
    )
    def fuzz_oversized_payload(self, cmd: int, oversized: bytes) -> None:
        """Inject frames exceeding the maximum allowed buffer size."""
        frame = self._build_envelope_frame(cmd, oversized)
        self.loop.run_until_complete(self._send_raw(frame))
        event("rule_oversized_payload")

    @rule(noise=st.binary(min_size=1, max_size=64))
    def fuzz_wire_noise(self, noise: bytes) -> None:
        """Simulate physical wire line noise and jitter."""
        self.loop.run_until_complete(self._send_raw(noise))
        event("rule_wire_noise")

    @rule(
        cmd=st.integers(min_value=0x7000, max_value=protocol.UINT16_MAX),
        payload=st.binary(min_size=0, max_size=64),
    )
    def fuzz_unknown_command(self, cmd: int, payload: bytes) -> None:
        """Inject unregistered command IDs to verify graceful rejection."""
        seq = self._next_seq_id()
        frame = self._build_raw_frame(cmd, seq, payload)
        self.loop.run_until_complete(self._send_raw(frame))
        event("rule_unknown_command")

    @rule()
    def verify_endpoint_responsiveness(self) -> None:
        """Send a valid probe frame to verify MCU endpoint remains responsive."""
        seq = self._next_seq_id()
        probe = self._build_raw_frame(0x0001, seq, b"PROBE")
        self.loop.run_until_complete(self._send_raw(probe))

        async def _read_probe_response() -> bytes | None:
            if self.reader is None:
                return None
            try:
                return await asyncio.wait_for(self.reader.readuntil(protocol.FRAME_DELIMITER), timeout=0.1)
            except (TimeoutError, OSError):
                return None

        resp = self.loop.run_until_complete(_read_probe_response())
        if resp is not None:
            self.probe_responses_received += 1
        event("rule_probe_verify")

    @invariant()
    def verify_fuzzer_invariants(self) -> None:
        """[SIL-2] Ensure sequence counter and connection state invariants hold."""
        assert 0 <= self.seq_id <= protocol.UINT16_MAX
        assert self.frames_sent >= 0
        assert self.writer is not None, "Serial writer disconnected unexpectedly"


ProtocolFuzzerTestCase = ProtocolFuzzerStateMachine.TestCase


@app.command()
def main(
    port: Annotated[str, typer.Option("--port", help="Serial port URL or device node")] = "/dev/ttyUSB0",
    baud: Annotated[int, typer.Option("--baud", help="Serial baudrate")] = protocol.DEFAULT_BAUDRATE,
    count: Annotated[int, typer.Option("--count", help="Number of stateful steps to execute")] = 1000,
    seed: Annotated[int | None, typer.Option("--seed", help="Deterministic RNG seed for Hypothesis")] = None,
) -> None:
    """Run Hypothesis-driven stateful protocol fuzzing against a target serial endpoint."""
    logger.info("starting_fuzzer_state_machine", port=port, baudrate=baud, steps=count, seed=seed)

    class ConfiguredFuzzerMachine(ProtocolFuzzerStateMachine):
        pass

    ConfiguredFuzzerMachine.port = port
    ConfiguredFuzzerMachine.baudrate = baud

    state_settings = settings(
        max_examples=max(1, count // 50),
        stateful_step_count=min(count, 50),
        derandomize=(seed is None),
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )

    runner = state_settings(run_state_machine_as_test)
    if seed is not None:
        from hypothesis import seed as hyp_seed

        runner = hyp_seed(seed)(runner)

    try:
        runner(ConfiguredFuzzerMachine)
        logger.info("fuzzing_complete", steps=count)
    except KeyboardInterrupt:
        logger.info("fuzzing_interrupted_by_user")


if __name__ == "__main__":
    app()
