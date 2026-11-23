#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Master Multi-Architecture Hardware Mesh Acceptance Runner.

Executes all 3 physical silicon emulators simultaneously:
1. Arduino Mega 2560 (8-bit AVR via simavr)
2. Arduino MKR WiFi 1010 (32-bit ARM Cortex-M0+ via Renode)
3. Arduino Nano ESP32 (32-bit Xtensa LX7 via Espressif QEMU)

All 3 emulators run the canonical universal reference sketch MeshFullServices.ino
and communicate concurrently with independent McuBridge daemons routing through a
single central ProtobufGateway.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, cast

import serial
import structlog
import tenacity
import typer
from google.protobuf.message import DecodeError
from pw_emu.frontend import Emulator
from pw_hdlc.decode import FrameDecoder
from pw_hdlc.encode import ui_frame
from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket
from mcubridge.config.logging import configure_logging
from mcubridge.protocol import protocol
from mcubridge.services.handshake import SerialHandshakeManager
from mcubridge_client import mcu_wire_pb2
from mcubridge_client.cli import bridge_session
from tools.arduino_core_metadata import ArduinoCoreMetadata, read_metadata_json, resolve_core_metadata
from tools.boards import (
    BOARD_MEGA,
    BOARD_MKRWIFI1010,
    BOARD_NANO_ESP32,
    BoardSpec,
)
from tools.emulation.emulation_runner import (
    CLOUD_HOST,
    CLOUD_PORT,
    DEFAULT_SHARED_SECRET,
    EmulationState,
    audit_post_execution_status,
    ensure_cloud_gateway,
    setup_fake_uci,
)
from tools.emulation.process_utils import (
    cleanup_paths,
    start_daemon_thread,
    stream_pump,
    terminate_process_tree,
)
from tools.emulation.qemu_runner import (
    find_qemu_binary,
    start_qemu_emulator,
    stop_qemu_emulator,
)
from tools.emulation.renode_runner import (
    find_renode_binary,
    start_renode_emulator,
    stop_renode_emulator,
)
from tools.emulation.simavr_runner import (
    build_simavr_runner,
    report_simavr_diagnostics,
    start_simavr_emulator,
    stop_simavr_emulator,
)

repo_root = Path(__file__).resolve().parents[2]

configure_logging(console=True)
logger = structlog.get_logger("fleet_mesh_hardware")

UART_READY_PROBE_INTERVAL_SECONDS = 0.5


@dataclass
class NodeRuntime:
    name: str
    board: BoardSpec
    metadata: ArduinoCoreMetadata
    firmware_path: Path
    temp_dir: Path
    status_file: Path
    pty_path: str | None = None
    procs: list[subprocess.Popen[str]] = field(default_factory=list[subprocess.Popen[str]])


def resolve_node_metadata(board: BoardSpec, build_dir: Path) -> ArduinoCoreMetadata:
    """Resolve board metadata from build directory JSON or core definitions (Rule 15 / Rule 27)."""
    meta_json = build_dir / "arduino_core_metadata.json"
    if meta_json.exists():
        return read_metadata_json(meta_json)
    return resolve_core_metadata(board.fqbn)


def ensure_firmware_compiled(sketch_path: Path, fqbn: str, output_path: Path) -> Path:
    """Compile firmware if missing using compile_firmware.sh."""
    if output_path.exists():
        return output_path
    logger.info("Compiling firmware for target", fqbn=fqbn, sketch=sketch_path.name)
    compile_script = repo_root / "tools" / "ci" / "compile_firmware.sh"
    cmd = [str(compile_script), str(sketch_path), fqbn]
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"Firmware compilation failed for {fqbn}")
    return output_path


def spawn_daemon_for_node(node: NodeRuntime, slave_name: str, log_collector: EmulationState) -> subprocess.Popen[str]:
    """Configure fake UCI and spawn an independent mcubridge daemon for a mesh node."""
    _, _, daemon_env = setup_fake_uci(
        slave_name,
        device_id=node.name,
        shared_secret=DEFAULT_SHARED_SECRET,
        base_dir=node.temp_dir,
        status_file=node.status_file,
        label=node.name,
    )
    daemon_cmd = [sys.executable, "-u", "-m", "mcubridge.daemon"]
    logger.info("Spawning mcubridge daemon for node", node=node.name, pty=slave_name)
    proc = subprocess.Popen(
        daemon_cmd,
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    start_daemon_thread(stream_pump, f"daemon-{node.name}", proc.stdout, log_collector.on_line, f"daemon-{node.name}")
    node.procs.append(proc)
    return proc


def wait_for_firmware_uart_ready(port: str, timeout: float) -> bool:
    """Wait for one authenticated LinkSync response without queuing overlapping probes."""
    sync_operation = protocol.get_operation("rpc.pb.SystemService/LinkSync")
    if sync_operation is None or sync_operation.service_id is None or sync_operation.method_id is None:
        raise RuntimeError("LinkSync is missing generated RPC metadata")
    if timeout <= 0:
        return False

    ser = serial.serial_for_url(
        port,
        baudrate=protocol.DEFAULT_SAFE_BAUDRATE,
        timeout=min(0.1, timeout),
        write_timeout=min(1.0, timeout),
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
        do_not_open=True,
    )
    decoder = FrameDecoder()
    secret = DEFAULT_SHARED_SECRET.encode("utf-8")
    pending_sync: tuple[int, bytes] | None = None
    call_id = 0
    next_probe = 0.0
    deadline = time.monotonic() + timeout

    try:
        ser.dtr = False
        ser.open()
        while time.monotonic() < deadline:
            now = time.monotonic()
            # Keep exactly one authenticated request in flight. On slower AVR
            # targets the MCU may queue UART responses until POST completes;
            # overlapping probes leave stale nonce responses for the daemon.
            if pending_sync is None and now >= next_probe:
                call_id = (call_id + 1) & protocol.UINT16_MAX or 1
                reset_request = ui_frame(
                    protocol.HDLC_ADDRESS_RPC,
                    protocol.encode_rpc_packet(
                        "rpc.pb.SystemService/LinkReset",
                        b"",
                        call_id=call_id,
                    ),
                )
                if ser.write(reset_request) != len(reset_request):
                    raise OSError("Partial write while resetting MCU link readiness probe")

                call_id = (call_id + 1) & protocol.UINT16_MAX or 1
                nonce = secrets.token_bytes(protocol.HANDSHAKE_NONCE_LENGTH)
                request_tag = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
                sync_request = ui_frame(
                    protocol.HDLC_ADDRESS_RPC,
                    protocol.encode_rpc_packet(
                        "rpc.pb.SystemService/LinkSync",
                        mcu_wire_pb2.LinkSync(nonce=nonce, tag=request_tag),
                        call_id=call_id,
                    ),
                )
                if ser.write(sync_request) != len(sync_request):
                    raise OSError("Partial write while probing MCU UART readiness")
                pending_sync = (call_id, nonce)

            for frame in decoder.process(ser.read(512)):
                if not frame.ok() or frame.address != protocol.HDLC_ADDRESS_RPC:
                    continue
                response = RpcPacket()
                try:
                    response.ParseFromString(frame.data)
                except DecodeError:
                    continue
                if pending_sync is None or response.call_id != pending_sync[0] or response.type != PacketType.RESPONSE:
                    continue

                expected_nonce = pending_sync[1]
                # This response completes the only outstanding probe, even if
                # authentication or RPC metadata is invalid. A fresh request
                # may be sent afterwards, but never while the previous one is pending.
                pending_sync = None
                next_probe = time.monotonic() + UART_READY_PROBE_INTERVAL_SECONDS
                if (
                    response.status != 0
                    or response.service_id != sync_operation.service_id
                    or response.method_id != sync_operation.method_id
                ):
                    continue
                try:
                    sync_response = mcu_wire_pb2.LinkSync.FromString(response.payload)
                except DecodeError:
                    continue
                # Handshake v2 (ADR-0005): the tag covers nonce || mcu_nonce.
                try:
                    expected_tag = SerialHandshakeManager.calculate_handshake_response_tag(
                        secret, expected_nonce, sync_response.mcu_nonce
                    )
                except ValueError:
                    continue
                if sync_response.nonce == expected_nonce and hmac.compare_digest(sync_response.tag, expected_tag):
                    logger.info("Firmware UART authenticated after POST", port=port)
                    return True
    finally:
        ser.close()

    return False


def wait_for_node_ready(status_file: Path, timeout: float = 30.0) -> bool:
    """Wait for node to complete both cryptographic synchronization and capabilities discovery."""

    def _is_ready() -> bool:
        if not status_file.exists():
            return False
        try:
            raw_obj = json.loads(status_file.read_text(encoding="utf-8"))
            if not isinstance(raw_obj, dict):
                return False
            data = cast(dict[str, object], raw_obj)
            bridge_obj = data.get("bridge")
            if isinstance(bridge_obj, dict):
                bridge = cast(dict[str, object], bridge_obj)
                is_sync = bridge.get("is_synchronized") is True or bridge.get("synchronised") is True
                serial_obj = bridge.get("serial_link")
                if isinstance(serial_obj, dict):
                    serial_link = cast(dict[str, object], serial_obj)
                    if serial_link.get("synchronised") is True:
                        is_sync = True
                caps_obj = bridge.get("capabilities")
                if isinstance(caps_obj, dict):
                    caps = cast(dict[str, object], caps_obj)
                    if is_sync and bool(caps):
                        return True
            return False
        except (json.JSONDecodeError, OSError):
            return False

    sync_retryer = tenacity.Retrying(
        stop=tenacity.stop_after_delay(timeout),
        wait=tenacity.wait_fixed(0.25),
        retry=tenacity.retry_if_result(lambda ok: not ok),
        reraise=False,
    )
    try:
        return sync_retryer(_is_ready)
    except tenacity.RetryError:
        return False


EMULATOR_LIVENESS_POLL_SECONDS = 0.5


def emulator_exit_reason(name: str, handle: Emulator) -> str | None:
    """Return a failure reason if a pw_emu Emulator has exited."""
    if not handle.running():
        return f"emulator {name} exited during verification"
    return None


async def verify_with_emulator_watchdog(
    nodes: tuple[NodeRuntime, ...],
    emulators: dict[str, Emulator],
) -> str | None:
    """Run mesh verification, aborting as soon as any emulator process exits.

    Returns None when verification passed, or a failure description. Without
    this watchdog a dead emulator only surfaces after every RPC retry times out.
    """
    verification = asyncio.create_task(verify_mesh_interoperability(nodes))
    while not verification.done():
        reason = next((r for name, handle in emulators.items() if (r := emulator_exit_reason(name, handle))), None)
        if reason is not None:
            verification.cancel()
            await asyncio.gather(verification, return_exceptions=True)
            return reason
        await asyncio.wait({verification}, timeout=EMULATOR_LIVENESS_POLL_SECONDS)
    return None if verification.result() else "cross-node mesh verification failed"


async def get_node_version_with_reconnect(node: NodeRuntime) -> mcu_wire_pb2.VersionResponse | None:
    """Probe GetVersion over a fresh Gateway session for each attempt.

    A timed-out Gateway dispatch may leave both the client socket and the
    device-side dispatch path unusable. Retrying the same stub then only
    produces ConnectionResetError/BrokenPipeError. Closing that session and
    reconnecting gives the Gateway a clean client session before retrying.
    """
    for attempt in range(5):
        try:
            async with bridge_session(
                host=CLOUD_HOST,
                port=CLOUD_PORT,
                device_id=node.name,
                timeout_ms=10000,
            ) as (_channel, stub):
                response = await stub.GetVersion(mcu_wire_pb2.SubscribeRequest())
            if response.major == 2 and response.minor == 8:
                logger.info(
                    "Node GetVersion verified",
                    node=node.name,
                    version=f"{response.major}.{response.minor}.{response.patch}",
                    attempt=attempt,
                )
                return response
            logger.warning(
                "GetVersion returned an unexpected version; reconnecting...",
                node=node.name,
                attempt=attempt,
                major=response.major,
                minor=response.minor,
            )
        except (RuntimeError, TimeoutError, ConnectionError, OSError) as exc:
            logger.warning(
                "GetVersion attempt failed; reconnecting with a fresh Gateway session...",
                node=node.name,
                attempt=attempt,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        if attempt < 4:
            await asyncio.sleep(1.0)
    logger.error("GetVersion failed after reconnect attempts", node=node.name)
    return None


async def verify_mesh_interoperability(nodes: tuple[NodeRuntime, ...]) -> bool:
    """Verify that all nodes are independently reachable, responsive to RPCs, and communicating."""
    logger.info("Executing cross-node RPC and mesh verification across all nodes simultaneously...")
    for node in nodes:
        logger.info("Verifying RPC services for node", node=node.name, board=node.board.label)
        ver_resp = await get_node_version_with_reconnect(node)
        if ver_resp is None:
            return False

        async with bridge_session(host=CLOUD_HOST, port=CLOUD_PORT, device_id=node.name, timeout_ms=60000) as (
            _channel,
            stub,
        ):
            mem_resp: mcu_wire_pb2.FreeMemoryResponse | None = None
            for attempt in range(5):
                try:
                    mem_resp = await stub.GetFreeMemory(mcu_wire_pb2.SubscribeRequest())
                    if mem_resp.value > 0:
                        break
                    logger.warning(
                        "GetFreeMemory returned 0, retrying...",
                        node=node.name,
                        attempt=attempt,
                    )
                except (RuntimeError, TimeoutError, ConnectionError, OSError) as exc:
                    logger.warning(
                        "GetFreeMemory attempt failed, retrying...",
                        node=node.name,
                        attempt=attempt,
                        error=str(exc),
                    )
                await asyncio.sleep(1.5)
            if mem_resp is None or mem_resp.value == 0:
                logger.error("Free memory reported as 0 or unavailable", node=node.name)
                return False
            logger.info("Node GetFreeMemory verified", node=node.name, free_bytes=mem_resp.value)

            led_pin = node.metadata.led_builtin
            for attempt in range(5):
                try:
                    await stub.DigitalWrite(mcu_wire_pb2.DigitalWrite(pin=led_pin, value=1))
                    await stub.DigitalWrite(mcu_wire_pb2.DigitalWrite(pin=led_pin, value=0))
                    break
                except (RuntimeError, TimeoutError, ConnectionError, OSError) as exc:
                    if attempt == 4:
                        logger.error("DigitalWrite failed", node=node.name, pin=led_pin, error=str(exc))
                        return False
                    await asyncio.sleep(1.5)
            logger.info("Node hardware actuation (LED) verified", node=node.name, pin=led_pin)

            for attempt in range(5):
                try:
                    await stub.MailboxPush(mcu_wire_pb2.MailboxPush(data=b"PING"))
                    break
                except (RuntimeError, TimeoutError, ConnectionError, OSError) as exc:
                    if attempt == 4:
                        logger.error("MailboxPush failed", node=node.name, error=str(exc))
                        return False
                    await asyncio.sleep(1.5)
            logger.info("Node MailboxPush verified", node=node.name)

            if node.board.fqbn.startswith("arduino:avr:") and not await verify_eeprom_round_trip(node, stub):
                return False

            if not await verify_file_upload(node):
                return False

    return True


# ADR-0007: a partial image keeps the emulated run short; the per-byte cost
# is logged so the full 4,080-byte write time can be extrapolated.
_EEPROM_PROBE = bytes((0xA5 ^ i) & 0xFF for i in range(128))


async def verify_eeprom_round_trip(node: NodeRuntime, stub: Any) -> bool:
    """Write the AVR EEPROM through client -> gateway -> daemon -> MCU and read it back."""
    started = time.monotonic()
    stage = "write"
    try:
        written = await stub.write_mcu_eeprom(_EEPROM_PROBE, timeout_ms=120000)
        elapsed = time.monotonic() - started
        stage = "read"
        image = await stub.read_mcu_eeprom(timeout_ms=120000)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        logger.error(
            "EEPROM round trip failed",
            node=node.name,
            stage=stage,
            error=str(exc),
            error_type=type(exc).__name__,
            error_repr=repr(exc),
        )
        return False
    if written != len(_EEPROM_PROBE) or image[: len(_EEPROM_PROBE)] != _EEPROM_PROBE:
        logger.error("EEPROM read-back mismatch", node=node.name, written=written, read=len(image))
        return False
    per_byte = elapsed / len(_EEPROM_PROBE)
    logger.info(
        "Node EEPROM round trip verified",
        node=node.name,
        write_seconds=round(elapsed, 2),
        image_bytes=len(image),
        extrapolated_full_write_seconds=round(per_byte * protocol.EEPROM_RESOURCE_MAX_BYTES, 1),
    )
    return True


_FILE_UPLOAD_PROBE = b"MCUBRIDGE_UPLOAD_E6"


async def verify_file_upload(node: NodeRuntime) -> bool:
    """Verify the sketch-driven MCU pw_transfer upload reached the MPU filesystem."""
    uploaded_path = node.temp_dir / "fs" / "fleet_upload_probe.bin"
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            content = uploaded_path.read_bytes()
        except OSError:
            await asyncio.sleep(0.1)
            continue
        if content == _FILE_UPLOAD_PROBE:
            logger.info(
                "Node MCU-to-Linux file upload verified",
                node=node.name,
                path=str(uploaded_path),
                size=len(content),
            )
            return True
        logger.error(
            "MCU-to-Linux upload content mismatch",
            node=node.name,
            path=str(uploaded_path),
            expected=len(_FILE_UPLOAD_PROBE),
            received=len(content),
        )
        return False
    logger.error(
        "MCU-to-Linux upload did not reach the MPU filesystem before timeout",
        node=node.name,
        path=str(uploaded_path),
        timeout_seconds=20,
    )
    return False


def compile_fleet_firmwares() -> (
    tuple[tuple[Path, ArduinoCoreMetadata], tuple[Path, ArduinoCoreMetadata], tuple[Path, ArduinoCoreMetadata]]
):
    """Compile universal firmwares and extract metadata for all 3 targets."""
    sketch_path = repo_root / "mcubridge-library-arduino" / "examples" / "MeshFullServices" / "MeshFullServices.ino"
    fw_mega = ensure_firmware_compiled(
        sketch_path, BOARD_MEGA.fqbn, repo_root / "build" / str(BOARD_MEGA.emulator) / BOARD_MEGA.slug / "firmware.elf"
    )
    meta_mega = resolve_node_metadata(BOARD_MEGA, fw_mega.parent)

    fw_mkr = ensure_firmware_compiled(
        sketch_path,
        BOARD_MKRWIFI1010.fqbn,
        repo_root / "build" / str(BOARD_MKRWIFI1010.emulator) / BOARD_MKRWIFI1010.slug / "firmware.elf",
    )
    meta_mkr = resolve_node_metadata(BOARD_MKRWIFI1010, fw_mkr.parent)

    fw_esp32 = ensure_firmware_compiled(
        sketch_path,
        BOARD_NANO_ESP32.fqbn,
        repo_root / "build" / str(BOARD_NANO_ESP32.emulator) / BOARD_NANO_ESP32.slug / "firmware_flash.bin",
    )
    meta_esp32 = resolve_node_metadata(BOARD_NANO_ESP32, fw_esp32.parent)
    return (fw_mega, meta_mega), (fw_mkr, meta_mkr), (fw_esp32, meta_esp32)


def verify_host_binaries() -> tuple[Path, str, str] | None:
    """Locate and verify all host emulation binaries (simavr_pty_runner, renode, qemu)."""
    runner_bin = build_simavr_runner()
    if not runner_bin:
        logger.error("pw_emu_avr simavr_pty_runner could not be built on host system")
        return None

    renode_bin = find_renode_binary()
    if not renode_bin:
        logger.error("Renode binary not available on host system")
        return None

    qemu_bin = find_qemu_binary()
    if not qemu_bin:
        logger.error("Espressif QEMU binary (esp32s3) not available on host system")
        return None

    return runner_bin, renode_bin, qemu_bin


def run_fleet_mesh_hardware_e2e(timeout_seconds: float = 180.0) -> bool:
    """Execute all 3 hardware emulators concurrently via Pigweed pw_emu and verify mesh operations."""
    logger.info("================================================================================")
    logger.info("Starting Master Fleet Mesh Hardware Emulation (pw_emu: simavr + Renode + QEMU)")
    logger.info("================================================================================")

    (fw_mega, meta_mega), (fw_mkr, meta_mkr), (fw_esp32, meta_esp32) = compile_fleet_firmwares()

    if verify_host_binaries() is None:
        return False

    base_temp_dir = Path(tempfile.mkdtemp(prefix="fleet_mesh_hw_"))
    log_collector = EmulationState()
    all_procs: list[subprocess.Popen[str]] = []
    simavr_emu: Emulator | None = None
    renode_emu: Emulator | None = None
    qemu_emu: Emulator | None = None
    simavr_wdir: Path | None = None
    node_mega: NodeRuntime | None = None
    node_mkr: NodeRuntime | None = None
    node_esp32: NodeRuntime | None = None

    try:
        gw_proc = ensure_cloud_gateway(log_collector)
        if gw_proc:
            all_procs.append(gw_proc)

        # Node 1: Arduino Mega 2560 (simavr via Pigweed pw_emu)
        node_mega = NodeRuntime(
            name="mega-01",
            board=BOARD_MEGA,
            metadata=meta_mega,
            firmware_path=fw_mega,
            temp_dir=base_temp_dir / BOARD_MEGA.slug,
            status_file=base_temp_dir / f"status_{BOARD_MEGA.slug}.json",
        )
        node_mega.temp_dir.mkdir(parents=True, exist_ok=True)
        simavr_wdir = node_mega.temp_dir / "pw_emu"
        simavr_emu, pty_mega = start_simavr_emulator(
            simavr_wdir,
            node_mega.firmware_path,
            node_mega.metadata.mcu,
            node_mega.metadata.frequency_hz,
            "0",
        )
        node_mega.pty_path = pty_mega

        # Node 2: Arduino MKR WiFi 1010 (Renode via Pigweed pw_emu)
        node_mkr = NodeRuntime(
            name="mkr-02",
            board=BOARD_MKRWIFI1010,
            metadata=meta_mkr,
            firmware_path=fw_mkr,
            temp_dir=base_temp_dir / BOARD_MKRWIFI1010.slug,
            status_file=base_temp_dir / f"status_{BOARD_MKRWIFI1010.slug}.json",
        )
        node_mkr.temp_dir.mkdir(parents=True, exist_ok=True)
        renode_wdir = node_mkr.temp_dir / "pw_emu"
        logger.info("Starting Renode via pw_emu for Node 2 (MKR WiFi 1010)")
        renode_emu, pty_mkr = start_renode_emulator(renode_wdir, node_mkr.firmware_path)
        node_mkr.pty_path = pty_mkr

        # Node 3: Arduino Nano ESP32 (QEMU via Pigweed pw_emu)
        node_esp32 = NodeRuntime(
            name="esp32-03",
            board=BOARD_NANO_ESP32,
            metadata=meta_esp32,
            firmware_path=fw_esp32,
            temp_dir=base_temp_dir / BOARD_NANO_ESP32.slug,
            status_file=base_temp_dir / f"status_{BOARD_NANO_ESP32.slug}.json",
        )
        node_esp32.temp_dir.mkdir(parents=True, exist_ok=True)
        qemu_wdir = node_esp32.temp_dir / "pw_emu"
        logger.info("Starting QEMU via pw_emu for Node 3 (Nano ESP32)")
        qemu_emu, pty_qemu = start_qemu_emulator(qemu_wdir, node_esp32.firmware_path, target=node_esp32.metadata.mcu)
        node_esp32.pty_path = pty_qemu

        nodes = (node_mega, node_mkr, node_esp32)
        ready_deadline = time.monotonic() + timeout_seconds
        logger.info("Waiting for all firmware POST checks to enable their UARTs...")
        for node in nodes:
            if node.pty_path is None:
                raise RuntimeError(f"No UART PTY configured for {node.name}")
            remaining = ready_deadline - time.monotonic()
            if remaining <= 0 or not wait_for_firmware_uart_ready(node.pty_path, timeout=remaining):
                logger.error(
                    "Firmware UART did not complete authenticated LinkSync after POST",
                    node=node.name,
                    board=node.board.label,
                )
                return False
            logger.info("Firmware POST passed and UART is responsive", node=node.name, board=node.board.label)

        for node in nodes:
            if node.pty_path is None:
                raise RuntimeError(f"No UART PTY configured for {node.name}")
            all_procs.append(spawn_daemon_for_node(node, node.pty_path, log_collector))

        sync_deadline = ready_deadline
        logger.info("Waiting for all 3 hardware emulators to complete cryptographic synchronization...")
        for node in nodes:
            remaining = sync_deadline - time.monotonic()
            if remaining <= 0:
                logger.error("Timed out waiting for all mesh nodes to synchronize")
                return False
            if not wait_for_node_ready(node.status_file, timeout=remaining):
                logger.error(
                    "Node failed cryptographic synchronization or capabilities discovery",
                    node=node.name,
                    board=node.board.label,
                )
                return False
            logger.info("Node synchronized successfully", node=node.name, board=node.board.label)

        emulators: dict[str, Emulator] = {
            "simavr": simavr_emu,
            "renode": renode_emu,
            "qemu": qemu_emu,
        }
        failure = asyncio.run(verify_with_emulator_watchdog(nodes, emulators))
        if failure is not None:
            logger.error("Cross-node mesh verification failed", reason=failure)
            return False

        logger.info("Performing Rule 29 Post-Execution Status Audit on all 3 nodes...")
        for node in nodes:
            if not node.status_file.exists() or not audit_post_execution_status(node.status_file):
                logger.error("Rule 29 health audit failed for node", node=node.name)
                return False
            logger.info("Rule 29 status audit passed with 0 errors", node=node.name)

        logger.info("================================================================================")
        logger.info("✅ SUCCESS: All 3 hardware emulations executed and communicated simultaneously!")
        logger.info("================================================================================")
        return True

    finally:
        logger.info("Tearing down all emulation subprocesses...")
        daemon_procs: list[subprocess.Popen[str]] = []
        for node_ref in (node_mega, node_mkr, node_esp32):
            if node_ref is not None:
                daemon_procs.extend(node_ref.procs)
        terminate_process_tree(daemon_procs, timeout=3.0)
        stop_simavr_emulator(simavr_emu)
        stop_renode_emulator(renode_emu)
        stop_qemu_emulator(qemu_emu)
        emulation_procs = [p for p in all_procs if p not in daemon_procs]
        terminate_process_tree(emulation_procs, timeout=3.0)
        if node_mega is not None and simavr_wdir is not None:
            # The runner persists diagnostics.json atomically on exit/crash;
            # symbolize it against the firmware ELF before cleaning up temp dirs.
            report_simavr_diagnostics(node_mega.firmware_path, simavr_wdir)
        cleanup_paths(base_temp_dir)


cli = typer.Typer(
    help="Master Multi-Architecture Hardware Mesh Acceptance Runner (simavr + Renode + QEMU).",
    add_completion=False,
)


@cli.command()
def main(
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="Timeout in seconds")] = 180.0,
) -> None:
    ok = run_fleet_mesh_hardware_e2e(timeout_seconds=timeout)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    cli()
