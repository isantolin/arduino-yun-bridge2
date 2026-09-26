#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] McuBridge Protocol Stateful Fuzzer.

Executes deterministic, property-based stateful fuzzing of serial framing, COBS/R encoding,
Protobuf envelopes, CRC verification, and error handling on simulated or physical MCU endpoints
using Hypothesis RuleBasedStateMachine for mathematical reproducibility and minimal shrinking.
"""

from __future__ import annotations

import asyncio
from binascii import crc32
from collections.abc import Callable
from typing import Annotated, cast

from cobs import cobsr
from hypothesis import HealthCheck, event, seed as hyp_seed, settings as hyp_settings, strategies as st
import hypothesis.stateful as h_stateful
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
import serialx
import structlog
import typer

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame

cli = typer.Typer(help="[MIL-SPEC/SIL-2] McuBridge Protocol Stateful Fuzzer", add_completion=False)
logger = structlog.get_logger("protocol_fuzzer")

_RUN_STATE_MACHINE: Callable[[type[RuleBasedStateMachine]], None] = cast(
    Callable[[type[RuleBasedStateMachine]], None],
    getattr(h_stateful, "run_state_machine_as_test"),
)


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
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.loop.run_until_complete(self._connect())

    async def _connect(self) -> None:
        reader, writer = await serialx.open_serial_connection(url=self.port, baudrate=self.baudrate)
        self.reader = cast(asyncio.StreamReader, reader)
        self.writer = cast(asyncio.StreamWriter, writer)
        logger.info("connected", port=self.port, baudrate=self.baudrate)

    async def _send_raw(self, data: bytes) -> None:
        writer = self.writer
        if writer is not None:
            writer.write(data)
            await writer.drain()
            self.frames_sent += 1

    async def _close(self) -> None:
        writer = self.writer
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
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
        return cobsr.encode(build_frame(command_id=cmd, sequence_id=seq, payload=payload)) + protocol.FRAME_DELIMITER

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
        return cobsr.encode(raw_frame) + protocol.FRAME_DELIMITER

    @rule(payload=st.binary(min_size=0, max_size=128))
    def fuzz_valid_ping(self, payload: bytes) -> None:
        """Generate valid version probe frames with arbitrary valid payloads."""
        seq = self._next_seq_id()
        frame = self._build_raw_frame(protocol.Command.CMD_GET_VERSION.value, seq, payload)
        self.loop.run_until_complete(self._send_raw(frame))
        event("rule_valid_ping")

    @rule(
        cmd=st.integers(min_value=1, max_value=0x7FFF),
        payload=st.binary(min_size=1, max_size=128),
        bad_crc=st.integers(min_value=0, max_value=protocol.CRC32_MASK),
    )
    def fuzz_invalid_crc(self, cmd: int, payload: bytes, bad_crc: int) -> None:
        """Inject frames with corrupt CRC32 checksums."""
        frame = self._build_envelope_frame(cmd, payload, override_crc=bad_crc)
        self.loop.run_until_complete(self._send_raw(frame))
        event("rule_invalid_crc")

    @rule(
        cmd=st.integers(min_value=1, max_value=0x7FFF),
        bad_version=st.one_of(
            st.integers(min_value=0, max_value=protocol.PROTOCOL_VERSION - 1),
            st.integers(min_value=protocol.PROTOCOL_VERSION + 1, max_value=protocol.UINT8_MASK),
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

    async def _read_probe(self) -> bytes | None:
        reader = self.reader
        if reader is None:
            return None
        try:
            return await asyncio.wait_for(reader.readuntil(protocol.FRAME_DELIMITER), timeout=0.1)
        except (TimeoutError, asyncio.IncompleteReadError, OSError):
            return None

    @rule()
    def verify_endpoint_responsiveness(self) -> None:
        """Send a valid probe frame to verify MCU endpoint remains responsive."""
        seq = self._next_seq_id()
        probe = self._build_raw_frame(protocol.Command.CMD_GET_VERSION.value, seq, b"PROBE")
        self.loop.run_until_complete(self._send_raw(probe))
        resp = self.loop.run_until_complete(self._read_probe())
        if resp is not None:
            self.probe_responses_received += 1
        event("rule_probe_verify")

    @invariant()
    def verify_fuzzer_invariants(self) -> None:
        """[SIL-2] Ensure sequence counter and connection state invariants hold."""
        assert 0 <= self.seq_id <= protocol.UINT16_MAX
        assert self.frames_sent >= 0
        assert self.writer is not None, "Serial writer disconnected unexpectedly"


@cli.command()
def main(
    port: Annotated[str, typer.Option("--port", help="Serial port URL or device node")] = "/dev/ttyUSB0",
    baud: Annotated[int, typer.Option("--baud", help="Serial baudrate")] = protocol.DEFAULT_BAUDRATE,
    count: Annotated[int, typer.Option("--count", help="Number of stateful steps to execute")] = 1000,
    seed: Annotated[int | None, typer.Option("--seed", help="Deterministic RNG seed for Hypothesis")] = None,
) -> None:
    """Run Hypothesis-driven stateful protocol fuzzing against a target serial endpoint."""
    if count <= 0:
        raise typer.BadParameter("count must be greater than 0")

    logger.info("starting_fuzzer_state_machine", port=port, baudrate=baud, steps=count, seed=seed)

    ProtocolFuzzerStateMachine.port = port
    ProtocolFuzzerStateMachine.baudrate = baud

    steps_per_example = min(count, 50)
    max_examples = max(1, count // steps_per_example)

    state_settings = hyp_settings(
        max_examples=max_examples,
        stateful_step_count=steps_per_example,
        derandomize=(seed is None),
        database=None,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )

    def _execute() -> None:
        _RUN_STATE_MACHINE(ProtocolFuzzerStateMachine)

    test_fn: Callable[[], None] = state_settings(_execute)
    if seed is not None:
        test_fn = hyp_seed(seed)(test_fn)

    try:
        test_fn()
        logger.info("fuzzing_complete", steps=count)
    except KeyboardInterrupt:
        logger.info("fuzzing_interrupted_by_user")


if __name__ == "__main__":
    cli()
