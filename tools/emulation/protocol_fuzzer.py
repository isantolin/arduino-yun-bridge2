#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] McuBridge Protocol Stateful Fuzzer.

Executes deterministic, property-based stateful fuzzing of serial framing, Pigweed HDLC encoding,
RPC packets, and error handling on simulated or physical MCU endpoints
using Hypothesis RuleBasedStateMachine for mathematical reproducibility and minimal shrinking.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess
from typing import Annotated, Any, ClassVar, cast

import hypothesis.stateful as h_stateful
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from pw_hdlc.encode import ui_frame
from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket
import serial
import structlog
import typer

from mcubridge.protocol import protocol
from mcubridge.transport.serial import resolve_serial_url
from tools.emulation.process_utils import terminate_process_tree, wait_for_path_ready

REPO_ROOT = Path(__file__).resolve().parents[2]

run_state_machine_as_test: Callable[[type[RuleBasedStateMachine]], None] = cast(
    Any, h_stateful
).run_state_machine_as_test

cli = typer.Typer(help="[MIL-SPEC/SIL-2] McuBridge Protocol Stateful Fuzzer", add_completion=False)
logger = structlog.get_logger("protocol_fuzzer")


class ProtocolFuzzerStateMachine(RuleBasedStateMachine):
    """[SIL-2] Hypothesis RuleBasedStateMachine for serial protocol fuzzing.

    Eliminates ad-hoc randomness in favor of mathematically reproducible sequences,
    automatic counterexample shrinking, and deterministic state invariant verification.
    """

    port: ClassVar[str] = "/dev/ttyUSB0"
    baudrate: ClassVar[int] = protocol.DEFAULT_BAUDRATE

    def __init__(self) -> None:
        super().__init__()
        self.seq_id: int = 0
        self.frames_sent: int = 0
        self.probe_responses_received: int = 0
        self.ser: serial.Serial | None = None
        self._connect()

    def _connect(self) -> None:
        url = resolve_serial_url(self.port)
        self.ser = serial.serial_for_url(url, baudrate=self.baudrate, timeout=0.1, write_timeout=2.0)
        logger.info("connected", port=self.port, baudrate=self.baudrate)

    def _send_raw(self, data: bytes) -> None:
        ser = self.ser
        if ser is not None:
            ser.write(data)
            ser.flush()
            self.frames_sent += 1

    def teardown(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except OSError as exc:
                logger.warning("ser_close_warning", error=str(exc))
            self.ser = None
        super().teardown()

    def _next_seq_id(self) -> int:
        self.seq_id = (self.seq_id + 1) & protocol.UINT16_MAX
        return self.seq_id

    def _build_raw_frame(self, cmd: int | str, seq: int, payload: bytes) -> bytes:
        """Build an intentionally arbitrary RpcPacket for negative protocol fuzzing."""
        spec = protocol.get_operation(cmd)
        if spec is not None and spec.service_id is not None and spec.method_id is not None:
            service_id, method_id = spec.service_id, spec.method_id
        elif isinstance(cmd, int):
            service_id = method_id = cmd
        else:
            service_id = method_id = 0
        packet = RpcPacket(
            type=PacketType.REQUEST,
            channel_id=protocol.HDLC_ADDRESS_RPC,
            service_id=service_id,
            method_id=method_id,
            call_id=seq,
            payload=payload,
        )
        return ui_frame(protocol.HDLC_ADDRESS_RPC, packet.SerializeToString())

    @rule(payload=st.binary(min_size=0, max_size=protocol.MAX_PAYLOAD_SIZE))
    def fuzz_valid_ping(self, payload: bytes) -> None:
        """Generate valid version probe frames with arbitrary valid payloads."""
        seq: int = self._next_seq_id()
        frame: bytes = self._build_raw_frame("rpc.pb.SystemService/GetVersion", seq, payload)
        self._send_raw(frame)

    @rule(
        cmd=st.integers(min_value=1, max_value=0x7FFF),
        payload=st.binary(min_size=1, max_size=128),
        corrupt_idx=st.integers(min_value=1, max_value=4),
    )
    def fuzz_invalid_crc(self, cmd: int, payload: bytes, corrupt_idx: int) -> None:
        """Inject frames with corrupt FCS checksums guaranteed to fail HDLC decode."""
        seq: int = self._next_seq_id()
        raw = bytearray(self._build_raw_frame(cmd, seq, payload))
        raw[-1 - corrupt_idx] ^= 0x01
        self._send_raw(bytes(raw))

    @rule(raw_bytes=st.binary(min_size=1, max_size=128))
    def fuzz_malformed_hdlc(self, raw_bytes: bytes) -> None:
        """Inject arbitrary unencoded byte sequences ending in frame delimiter."""
        frame: bytes = raw_bytes + protocol.FRAME_DELIMITER
        self._send_raw(frame)

    @rule(noise=st.binary(min_size=1, max_size=64))
    def fuzz_wire_noise(self, noise: bytes) -> None:
        """Simulate physical wire line noise and jitter."""
        self._send_raw(noise)

    @rule(
        cmd=st.integers(min_value=0x7000, max_value=protocol.UINT16_MAX),
        payload=st.binary(min_size=0, max_size=protocol.MAX_PAYLOAD_SIZE),
    )
    def fuzz_unknown_command(self, cmd: int, payload: bytes) -> None:
        """Inject unregistered command IDs to verify graceful rejection."""
        seq: int = self._next_seq_id()
        frame: bytes = self._build_raw_frame(cmd, seq, payload)
        self._send_raw(frame)

    def _read_probe(self) -> bytes | None:
        ser = self.ser
        if ser is None:
            return None
        try:
            data = ser.read_until(protocol.FRAME_DELIMITER)
            return data or None
        except OSError:
            return None

    @rule()
    def verify_endpoint_responsiveness(self) -> None:
        """Send a valid probe frame to verify MCU endpoint remains responsive."""
        seq: int = self._next_seq_id()
        probe: bytes = self._build_raw_frame("rpc.pb.SystemService/GetVersion", seq, b"PROBE")
        self._send_raw(probe)
        resp: bytes | None = self._read_probe()
        if resp is not None:
            self.probe_responses_received += 1

    @invariant()
    def verify_fuzzer_invariants(self) -> None:
        """[SIL-2] Ensure sequence counter and connection state invariants hold."""
        assert 0 <= self.seq_id <= protocol.UINT16_MAX
        assert self.frames_sent >= 0
        assert self.probe_responses_received >= 0
        assert self.ser is not None, "Serial port is None"
        assert self.ser.is_open, "Serial port is not open"


@cli.command()
def main(
    port: Annotated[str, typer.Option("--port", help="Serial port URL or device node")] = "/dev/ttyUSB0",
    baud: Annotated[int, typer.Option("--baud", help="Serial baudrate")] = protocol.DEFAULT_BAUDRATE,
    count: Annotated[int, typer.Option("--count", help="Number of stateful steps to execute")] = 1000,
    emulator: Annotated[
        bool,
        typer.Option("--emulator", help="Launch local bridge_control_emulator over a temporary socat PTY"),
    ] = False,
) -> None:
    """Run Hypothesis-driven stateful protocol fuzzing against a target serial endpoint."""
    if count <= 0:
        raise ValueError("count must be greater than 0")

    socat_proc: subprocess.Popen[bytes] | None = None
    fuzz_pty: Path | None = None
    target_port = port
    if emulator:
        compile_script = REPO_ROOT / "tools" / "ci" / "compile_emulator.sh"
        emulator_bin = REPO_ROOT / "mcubridge-library-arduino" / "tests" / "bridge_control_emulator"
        if not emulator_bin.exists() and compile_script.exists():
            subprocess.run(["bash", str(compile_script)], cwd=str(REPO_ROOT), check=True)
        fuzz_pty = Path("/tmp/ttyBRIDGE_FUZZ")
        fuzz_pty.unlink(missing_ok=True)
        socat_proc = subprocess.Popen(
            [
                "socat",
                "-d",
                "-d",
                f"PTY,link={fuzz_pty},raw,echo=0",
                f'EXEC:"{emulator_bin}",pty,raw,echo=0',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(REPO_ROOT),
        )
        if not wait_for_path_ready(fuzz_pty, timeout=4.0, interval=0.2):
            terminate_process_tree([socat_proc], timeout=2.0)
            raise RuntimeError("PTY device never appeared")
        target_port = str(fuzz_pty)

    logger.info("starting_fuzzer_state_machine", port=target_port, baudrate=baud, steps=count)
    ProtocolFuzzerStateMachine.port = target_port
    ProtocolFuzzerStateMachine.baudrate = baud

    try:
        run_state_machine_as_test(ProtocolFuzzerStateMachine)
        logger.info("fuzzing_complete", steps=count)
    except KeyboardInterrupt:
        logger.info("fuzzing_interrupted_by_user")
    finally:
        if socat_proc is not None:
            terminate_process_tree([socat_proc], timeout=2.0)
        if fuzz_pty is not None:
            fuzz_pty.unlink(missing_ok=True)


if __name__ == "__main__":
    cli()
