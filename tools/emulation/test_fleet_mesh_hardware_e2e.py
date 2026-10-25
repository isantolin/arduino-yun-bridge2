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
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import structlog
import typer
from mcubridge.config.logging import configure_logging
from mcubridge.protocol import protocol
from mcubridge_client import cloud_gateway_pb2, mcu_wire_pb2
from mcubridge_client.cli import bridge_session
from tools.arduino_core_metadata import ArduinoCoreMetadata, read_metadata_json, resolve_core_metadata
from tools.audit.audit_bridge_status import audit_status_dict
from tools.boards import (
    BOARD_MEGA,
    BOARD_MKRWIFI1010,
    BOARD_NANO_ESP32,
    BoardSpec,
)
from tools.emulation.emulation_runner import EmulationState, ensure_cloud_gateway, wait_for_daemon_sync
from tools.emulation.process_utils import (
    start_daemon_thread,
    stream_pump,
    terminate_process_tree,
    wait_for_path_ready,
    wait_for_tcp_ready,
    write_fake_uci_module,
)
from tools.emulation.qemu_runner import find_qemu_binary
from tools.emulation.simavr_runner import (
    SimavrState,
    build_simavr_harness,
    spawn_simavr,
)

repo_root = Path(__file__).resolve().parents[2]

configure_logging(console=True)
logger = structlog.get_logger("fleet_mesh_hardware")

# --- Network & IPC Constants ---
CLOUD_HOST = "127.0.0.1"
CLOUD_PORT = protocol.DEFAULT_CLOUD_PORT
PORT_RENODE = 12345
PORT_QEMU = 12346
SHARED_SECRET = "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"


@dataclass
class FleetMeshState(EmulationState):
    qemu_listening_event: threading.Event = field(default_factory=threading.Event)

    def on_line(self, line: str, source: str) -> None:
        if source == "qemu" and "QEMU waiting for connection" in line:
            self.qemu_listening_event.set()
        clean_line = line.strip()
        if not clean_line:
            return
        if source == "renode" and (
            "Unhandled read from offset" in clean_line or "Unhandled write to offset" in clean_line
        ):
            return
        super().on_line(line, source)


@dataclass
class NodeRuntime:
    name: str
    board: BoardSpec
    metadata: ArduinoCoreMetadata
    firmware_path: Path
    temp_dir: Path
    status_file: Path
    pty_path: Path | None = None
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
    fake_uci_dir = node.temp_dir / "uci"
    fake_uci_dir.mkdir(parents=True, exist_ok=True)
    storage_path = node.temp_dir / "storage"
    storage_path.mkdir(parents=True, exist_ok=True)
    spool_path = node.temp_dir / "spool"
    spool_path.mkdir(parents=True, exist_ok=True)
    fs_root = node.temp_dir / "fs"
    fs_root.mkdir(parents=True, exist_ok=True)

    uci_config = {
        "serial_port": slave_name,
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
        "cloud_enabled": "1",
        "cloud_host": CLOUD_HOST,
        "cloud_port": str(CLOUD_PORT),
        "cloud_tls": "0",
        "cloud_tls_insecure": "1",
        "watchdog_enabled": "0",
        "serial_response_timeout": "30.0",
        "serial_shared_secret": SHARED_SECRET,
        "allowed_commands": "*",
        "storage_path": str(storage_path),
        "cloud_spool_dir": str(spool_path),
        "file_system_root": str(fs_root),
        "debug": "1",
    }
    write_fake_uci_module(fake_uci_dir, uci_config, label=node.name)

    daemon_env = dict(os.environ)
    extra_paths = [
        str(fake_uci_dir),
        str(repo_root / "mcubridge"),
        str(repo_root / "mcubridge-client-examples"),
        str(repo_root),
    ]
    curr_pp = daemon_env.get("PYTHONPATH", "")
    daemon_env["PYTHONPATH"] = ":".join(extra_paths + ([curr_pp] if curr_pp else []))
    daemon_env["PYTHONUNBUFFERED"] = "1"
    daemon_env["MCUBRIDGE_FORCE_UCI"] = "1"
    daemon_env["MCUBRIDGE_NON_INTERACTIVE"] = "1"
    daemon_env["MCUBRIDGE_LOG_STREAM"] = "1"
    daemon_env["MCUBRIDGE_DEVICE_ID"] = node.name
    daemon_env["MCUBRIDGE_STATUS_FILE"] = str(node.status_file)
    daemon_env["MCUBRIDGE_SERIAL_PORT"] = slave_name
    daemon_env["MCUBRIDGE_STORAGE_PATH"] = str(storage_path)
    daemon_env["MCUBRIDGE_CLOUD_SPOOL_DIR"] = str(spool_path)
    daemon_env["MCUBRIDGE_FILE_SYSTEM_ROOT"] = str(fs_root)

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


async def verify_mesh_interoperability(nodes: tuple[NodeRuntime, ...]) -> bool:
    """Verify that all nodes are independently reachable, responsive to RPCs, and communicating."""
    logger.info("Executing cross-node RPC and mesh verification across all nodes simultaneously...")
    for node in nodes:
        logger.info("Verifying RPC services for node", node=node.name, board=node.board.label)
        async with bridge_session(host=CLOUD_HOST, port=CLOUD_PORT, device_id=node.name) as (_channel, stub):
            # 1. System Version Introspection
            ver_resp = await stub.GetVersion(cloud_gateway_pb2.SubscribeRequest())
            if ver_resp.major != 2 or ver_resp.minor != 8:
                logger.error("Unexpected version response", node=node.name, major=ver_resp.major, minor=ver_resp.minor)
                return False
            logger.info(
                "Node GetVersion verified",
                node=node.name,
                version=f"{ver_resp.major}.{ver_resp.minor}.{ver_resp.patch}",
            )

            # 2. Free Memory Introspection
            mem_resp = await stub.GetFreeMemory(cloud_gateway_pb2.SubscribeRequest())
            if mem_resp.value == 0:
                logger.error("Free memory reported as 0", node=node.name)
                return False
            logger.info("Node GetFreeMemory verified", node=node.name, free_bytes=mem_resp.value)

            # 3. Hardware Actuation (LED Builtin Toggle)
            led_pin = node.metadata.led_builtin
            write_resp = await stub.DigitalWrite(mcu_wire_pb2.DigitalWrite(pin=led_pin, value=1))
            if write_resp.status != "ok":
                logger.error("DigitalWrite failed", node=node.name, pin=led_pin)
                return False
            await stub.DigitalWrite(mcu_wire_pb2.DigitalWrite(pin=led_pin, value=0))
            logger.info("Node hardware actuation (LED) verified", node=node.name, pin=led_pin)

            # 4. Mailbox Messaging
            mb_resp = await stub.MailboxPush(mcu_wire_pb2.MailboxPush(data=b"PING"))
            if mb_resp.status != "ok":
                logger.error("MailboxPush failed", node=node.name)
                return False
            logger.info("Node MailboxPush verified", node=node.name)

    return True


def run_fleet_mesh_hardware_e2e(timeout_seconds: float = 120.0) -> bool:
    """Execute all 3 hardware emulators concurrently and verify cross-node mesh operations."""
    logger.info("================================================================================")
    logger.info("Starting Master Fleet Mesh Hardware Emulation (simavr + Renode + QEMU)")
    logger.info("================================================================================")

    # 1. Compile universal firmwares (which installs Arduino libraries if missing)
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

    # 2. Locate and verify emulator binaries
    simavr_bin = shutil.which("simavr")
    harness_bin = build_simavr_harness()
    if not simavr_bin or not harness_bin:
        logger.error("simavr or simavr_harness not available on host system")
        return False

    renode_bin = shutil.which("renode")
    if not renode_bin:
        for candidate in ["/usr/local/bin/renode", "/opt/renode/renode"]:
            if Path(candidate).exists():
                renode_bin = candidate
                break
    if not renode_bin:
        logger.error("Renode binary not available on host system")
        return False

    qemu_bin = find_qemu_binary()
    if not qemu_bin:
        logger.error("Espressif QEMU binary (esp32s3) not available on host system")
        return False

    socat_bin = shutil.which("socat")
    if not socat_bin:
        logger.error("socat binary not found on host system")
        return False

    base_temp_dir = Path(tempfile.mkdtemp(prefix="fleet_mesh_hw_"))
    log_collector = FleetMeshState()
    all_procs: list[subprocess.Popen[str]] = []
    node_mega: NodeRuntime | None = None
    node_mkr: NodeRuntime | None = None
    node_esp32: NodeRuntime | None = None

    try:
        # 3. Start central ProtobufGateway
        gw_proc = ensure_cloud_gateway(log_collector)
        if gw_proc:
            all_procs.append(gw_proc)

        # 4. Initialize Node 1: Arduino Mega 2560 (simavr)
        node_mega = NodeRuntime(
            name="mega-01",
            board=BOARD_MEGA,
            metadata=meta_mega,
            firmware_path=fw_mega,
            temp_dir=base_temp_dir / BOARD_MEGA.slug,
            status_file=base_temp_dir / f"status_{BOARD_MEGA.slug}.json",
        )
        node_mega.temp_dir.mkdir(parents=True, exist_ok=True)

        class FleetSimavrState(SimavrState):
            def on_line(self, line: str, source: str) -> None:
                super().on_line(line, source)
                log_collector.on_line(line, source)

        simavr_state = FleetSimavrState()
        simavr_proc, pty_mega, _ = spawn_simavr(
            harness_bin,
            node_mega.firmware_path,
            node_mega.metadata.mcu,
            node_mega.metadata.frequency_hz,
            "0",
            simavr_state,
        )
        if not simavr_proc or not pty_mega:
            raise RuntimeError("simavr failed to spawn or emit a valid PTY slave path")
        all_procs.append(simavr_proc)
        d_mega = spawn_daemon_for_node(node_mega, pty_mega, log_collector)
        all_procs.append(d_mega)

        # 5. Initialize Node 2: Arduino MKR WiFi 1010 (Renode)
        node_mkr = NodeRuntime(
            name="mkr-02",
            board=BOARD_MKRWIFI1010,
            metadata=meta_mkr,
            firmware_path=fw_mkr,
            temp_dir=base_temp_dir / BOARD_MKRWIFI1010.slug,
            status_file=base_temp_dir / f"status_{BOARD_MKRWIFI1010.slug}.json",
        )
        node_mkr.temp_dir.mkdir(parents=True, exist_ok=True)
        resc_template = repo_root / "tools" / "emulation" / "renode" / "mkrwifi1010.resc"
        repl_template = repo_root / "tools" / "emulation" / "renode" / "mkrwifi1010.repl"
        rendered_resc = (
            resc_template.read_text(encoding="utf-8")
            .replace("$ELF_PATH", f"@{node_mkr.firmware_path.resolve()}")
            .replace("$UART_PORT", str(PORT_RENODE))
            .replace("$ORIGIN/mkrwifi1010.repl", f"@{repl_template.resolve()}")
        )
        temp_resc = node_mkr.temp_dir / "run.resc"
        temp_resc.write_text(f"logFile @{(node_mkr.temp_dir / 'renode.log').resolve()} true\n{rendered_resc}\n")
        renode_cmd = [
            renode_bin,
            "--disable-gui",
            "--hide-monitor",
            "--hide-analyzers",
            "-P",
            "-1",
            "--plain",
            "-e",
            f"include @{temp_resc.resolve()}",
        ]
        logger.info("Spawning Renode for Node 2 (MKR WiFi 1010)", port=PORT_RENODE)
        renode_proc = subprocess.Popen(renode_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        all_procs.append(renode_proc)
        start_daemon_thread(stream_pump, "renode", renode_proc.stdout, log_collector.on_line, "renode")

        if not wait_for_tcp_ready(CLOUD_HOST, PORT_RENODE, timeout=30.0):
            raise RuntimeError(f"Renode UART socket terminal failed to become ready on port {PORT_RENODE}")

        pty_mkr = node_mkr.temp_dir / "ttyRENODE"
        socat_mkr_cmd = [
            socat_bin,
            "-d",
            "-d",
            f"pty,link={pty_mkr},raw,echo=0",
            f"tcp:{CLOUD_HOST}:{PORT_RENODE},retry=30,interval=0.2",
        ]
        socat_mkr_proc = subprocess.Popen(socat_mkr_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        all_procs.append(socat_mkr_proc)
        start_daemon_thread(stream_pump, "socat-mkr", socat_mkr_proc.stdout, log_collector.on_line, "socat-mkr")

        if not wait_for_path_ready(pty_mkr, timeout=15.0):
            raise RuntimeError("Timed out waiting for Renode PTY bridge")
        d_mkr = spawn_daemon_for_node(node_mkr, str(pty_mkr), log_collector)
        all_procs.append(d_mkr)

        # 6. Initialize Node 3: Arduino Nano ESP32 (QEMU)
        node_esp32 = NodeRuntime(
            name="esp32-03",
            board=BOARD_NANO_ESP32,
            metadata=meta_esp32,
            firmware_path=fw_esp32,
            temp_dir=base_temp_dir / BOARD_NANO_ESP32.slug,
            status_file=base_temp_dir / f"status_{BOARD_NANO_ESP32.slug}.json",
        )
        node_esp32.temp_dir.mkdir(parents=True, exist_ok=True)
        qemu_cmd = [
            qemu_bin,
            "-nographic",
            "-monitor",
            "none",
            "-machine",
            node_esp32.metadata.mcu,
            "-drive",
            f"file={node_esp32.firmware_path},if=mtd,format=raw",
            "-serial",
            f"tcp:{CLOUD_HOST}:{PORT_QEMU},server,wait",
        ]
        logger.info("Spawning QEMU for Node 3 (Nano ESP32)", port=PORT_QEMU)
        qemu_proc = subprocess.Popen(qemu_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        all_procs.append(qemu_proc)
        start_daemon_thread(stream_pump, "qemu", qemu_proc.stdout, log_collector.on_line, "qemu")

        if not log_collector.qemu_listening_event.wait(timeout=15.0):
            raise RuntimeError("QEMU UART TCP server failed to become ready")

        pty_esp32 = node_esp32.temp_dir / "ttyQEMU"
        socat_esp32_cmd = [
            socat_bin,
            "-d",
            "-d",
            f"pty,link={pty_esp32},raw,echo=0",
            f"tcp:{CLOUD_HOST}:{PORT_QEMU},retry=30,interval=0.2",
        ]
        socat_esp32_proc = subprocess.Popen(
            socat_esp32_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        all_procs.append(socat_esp32_proc)
        start_daemon_thread(stream_pump, "socat-esp32", socat_esp32_proc.stdout, log_collector.on_line, "socat-esp32")

        if not wait_for_path_ready(pty_esp32, timeout=15.0):
            raise RuntimeError("Timed out waiting for QEMU PTY bridge")
        d_esp32 = spawn_daemon_for_node(node_esp32, str(pty_esp32), log_collector)
        all_procs.append(d_esp32)

        # -------------------------------------------------------------------------
        # 7. CONCURRENT SYNCHRONIZATION GATE (All 3 nodes syncing in parallel)
        # -------------------------------------------------------------------------
        logger.info("Waiting for all 3 hardware emulators to complete cryptographic synchronization...")
        nodes = (node_mega, node_mkr, node_esp32)
        start_time = time.time()
        for node in nodes:
            remaining = max(1.0, timeout_seconds - (time.time() - start_time))
            if not wait_for_daemon_sync(node.status_file, timeout=remaining):
                logger.error("Node failed cryptographic synchronization", node=node.name, board=node.board.label)
                return False
            logger.info("Node synchronized successfully", node=node.name, board=node.board.label)

        # -------------------------------------------------------------------------
        # 8. CROSS-NODE DIALOG AND INTEROPERABILITY VERIFICATION
        # -------------------------------------------------------------------------
        if not asyncio.run(verify_mesh_interoperability(nodes)):
            logger.error("Cross-node mesh verification failed")
            return False

        # -------------------------------------------------------------------------
        # 9. POST-EXECUTION STATUS HEALTH AUDIT (Rule 29)
        # -------------------------------------------------------------------------
        logger.info("Performing Rule 29 Post-Execution Status Audit on all 3 nodes...")
        for node in nodes:
            if not node.status_file.exists():
                logger.error("Status file does not exist", node=node.name, path=str(node.status_file))
                return False
            status_data = json.loads(node.status_file.read_text(encoding="utf-8"))
            errors = audit_status_dict(status_data)
            if errors:
                logger.error("Rule 29 health audit failed for node", node=node.name, errors=errors)
                return False
            logger.info("Rule 29 status audit passed with 0 errors", node=node.name)

        logger.info("================================================================================")
        logger.info("✅ SUCCESS: All 3 hardware emulations executed and communicated simultaneously!")
        logger.info("================================================================================")
        return True

    finally:
        logger.info("Tearing down all emulation subprocesses...")
        daemon_procs: list[subprocess.Popen[str]] = []
        if node_mega is not None:
            daemon_procs.extend(node_mega.procs)
        if node_mkr is not None:
            daemon_procs.extend(node_mkr.procs)
        if node_esp32 is not None:
            daemon_procs.extend(node_esp32.procs)
        terminate_process_tree(daemon_procs, timeout=3.0)
        emulation_procs = [p for p in all_procs if p not in daemon_procs]
        terminate_process_tree(emulation_procs, timeout=3.0)
        shutil.rmtree(base_temp_dir, ignore_errors=True)


cli = typer.Typer(
    help="Master Multi-Architecture Hardware Mesh Acceptance Runner (simavr + Renode + QEMU).",
    add_completion=False,
)


@cli.command()
def main(
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="Timeout in seconds")] = 120.0,
) -> None:
    ok = run_fleet_mesh_hardware_e2e(timeout_seconds=timeout)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    cli()
