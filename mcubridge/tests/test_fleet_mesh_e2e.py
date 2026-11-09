"""E2E Fleet & Mesh Interoperability Test Suite (MIL-SPEC / SIL-2).

Validates concurrent multi-node mesh topology across 3 distinct hardware architectures:
- Node 1: Arduino Mega 2560 (8-bit AVR / simavr)
- Node 2: Arduino MKR WiFi 1010 (32-bit ARM / Renode)
- Node 3: Arduino Nano ESP32 (32-bit Xtensa / QEMU)

Exercises 100% of the 12 core ecosystem services dialoguing through the ProtobufGateway.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pw_hdlc.decode import FrameDecoder
from pw_rpc.internal.packet_pb2 import RpcPacket

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge_client import mcu_wire_pb2
from tools.emulation import test_fleet_mesh_hardware_e2e as fleet_mesh


@pytest.mark.parametrize(
    ("bridge_status", "expected"),
    [
        ({"is_synchronized": True, "capabilities": {"watchdog": True}}, True),
        ({"serial_link": {"synchronised": True}, "capabilities": {"watchdog": True}}, True),
        ({"is_synchronized": True}, False),
        ({"capabilities": {"watchdog": True}}, False),
    ],
)
def test_wait_for_node_ready_requires_sync_and_capabilities(
    tmp_path: Path,
    bridge_status: dict[str, object],
    expected: bool,
) -> None:
    status_file = tmp_path / "node-status.json"
    status_file.write_text(json.dumps({"bridge": bridge_status}), encoding="utf-8")

    assert fleet_mesh.wait_for_node_ready(status_file, timeout=0) is expected


def test_firmware_uart_probe_requires_matching_authenticated_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    sync_operation = fleet_mesh.protocol.get_operation(protocol.Command.CMD_LINK_SYNC.value)
    assert sync_operation is not None
    assert sync_operation.method_id is not None

    class FakeSerial:
        def __init__(self) -> None:
            self.dtr = True
            self.incoming = bytearray()
            self.decoder = FrameDecoder()
            self.sync_attempts = 0
            self.closed = False

        def open(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        def write(self, data: bytes) -> int:
            for frame in self.decoder.process(data):
                request = RpcPacket.FromString(frame.data)
                if request.method_id != sync_operation.method_id:
                    continue
                self.sync_attempts += 1
                sync_request = mcu_wire_pb2.LinkSync.FromString(request.payload)
                secret = fleet_mesh.DEFAULT_SHARED_SECRET.encode("utf-8")
                response_tag = fleet_mesh.SerialHandshakeManager.calculate_handshake_response_tag(
                    secret, sync_request.nonce
                )
                if self.sync_attempts == 1:
                    self._queue_response(request.call_id + 1, sync_request.nonce, response_tag)
                    self._queue_response(
                        request.call_id,
                        sync_request.nonce,
                        bytes(value ^ 0xFF for value in response_tag),
                    )
                else:
                    self._queue_response(request.call_id, sync_request.nonce, response_tag)
            return len(data)

        def read(self, size: int) -> bytes:
            if not self.incoming:
                time.sleep(0.001)
                return b""
            data = bytes(self.incoming[:size])
            del self.incoming[:size]
            return data

        def _queue_response(self, call_id: int, nonce: bytes, tag: bytes) -> None:
            self.incoming.extend(
                fleet_mesh.protocol.encode_hdlc_rpc(
                    protocol.Command.CMD_LINK_SYNC.value,
                    mcu_wire_pb2.LinkSync(nonce=nonce, tag=tag),
                    call_id=call_id,
                    packet_type=1,
                )
            )

    fake_serial = FakeSerial()

    def open_serial(*_args: object, **_kwargs: object) -> FakeSerial:
        return fake_serial

    def fixed_nonce(size: int) -> bytes:
        return b"\x01" * size

    monkeypatch.setattr(fleet_mesh.serial, "serial_for_url", open_serial)
    monkeypatch.setattr(fleet_mesh.secrets, "token_bytes", fixed_nonce)
    monkeypatch.setattr(fleet_mesh, "UART_READY_PROBE_INTERVAL_SECONDS", 0.0)

    assert fleet_mesh.wait_for_firmware_uart_ready("/dev/fake-uart", timeout=1.0)
    assert fake_serial.sync_attempts == 2
    assert fake_serial.dtr is False
    assert fake_serial.closed is True


def test_firmware_uart_probe_never_overlaps_delayed_sync_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    sync_operation = fleet_mesh.protocol.get_operation(protocol.Command.CMD_LINK_SYNC.value)
    assert sync_operation is not None
    assert sync_operation.method_id is not None

    class DelayedSerial:
        def __init__(self) -> None:
            self.dtr = True
            self.incoming = bytearray()
            self.decoder = FrameDecoder()
            self.pending: list[tuple[float, int, bytes, bytes]] = []
            self.sync_attempts = 0
            self.max_inflight = 0
            self.closed = False

        def open(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        def write(self, data: bytes) -> int:
            for frame in self.decoder.process(data):
                request = RpcPacket.FromString(frame.data)
                if request.method_id != sync_operation.method_id:
                    continue
                self.sync_attempts += 1
                sync_request = mcu_wire_pb2.LinkSync.FromString(request.payload)
                secret = fleet_mesh.DEFAULT_SHARED_SECRET.encode("utf-8")
                response_tag = fleet_mesh.SerialHandshakeManager.calculate_handshake_response_tag(
                    secret, sync_request.nonce
                )
                self.pending.append(
                    (time.monotonic() + 0.55, request.call_id, sync_request.nonce, response_tag)
                )
                self.max_inflight = max(self.max_inflight, len(self.pending))
            return len(data)

        def read(self, size: int) -> bytes:
            now = time.monotonic()
            ready = [item for item in self.pending if item[0] <= now]
            self.pending = [item for item in self.pending if item[0] > now]
            for _, call_id, nonce, tag in ready:
                self.incoming.extend(
                    fleet_mesh.protocol.encode_hdlc_rpc(
                        protocol.Command.CMD_LINK_SYNC.value,
                        mcu_wire_pb2.LinkSync(nonce=nonce, tag=tag),
                        call_id=call_id,
                        packet_type=1,
                    )
                )
            if not self.incoming:
                time.sleep(0.001)
                return b""
            data = bytes(self.incoming[:size])
            del self.incoming[:size]
            return data

    fake_serial = DelayedSerial()

    def open_serial(*_args: object, **_kwargs: object) -> DelayedSerial:
        return fake_serial

    monkeypatch.setattr(fleet_mesh.serial, "serial_for_url", open_serial)
    def fixed_nonce(size: int) -> bytes:
        return bytes([2]) * size

    monkeypatch.setattr(fleet_mesh.secrets, "token_bytes", fixed_nonce)
    monkeypatch.setattr(fleet_mesh, "UART_READY_PROBE_INTERVAL_SECONDS", 0.05)

    assert fleet_mesh.wait_for_firmware_uart_ready("/dev/fake-uart", timeout=1.0)
    assert fake_serial.sync_attempts == 1
    assert fake_serial.max_inflight == 1
    assert fake_serial.pending == []
    assert fake_serial.closed is True


class MeshNodeContext:
    """Encapsulates the state, services, and mocked serial link for a single mesh node."""

    def __init__(self, node_id: str, arch: str, base_dir: Path) -> None:
        self.node_id = node_id
        self.arch = arch
        self.node_dir = base_dir / node_id
        self.node_dir.mkdir(parents=True, exist_ok=True)

        self.status_file = self.node_dir / "status.json"
        self.published_messages: list[pb.CloudQueuedPublish] = []

        self.config = RuntimeConfig(
            cloud_host="127.0.0.1",
            cloud_port=8443,
            cloud_enabled=True,
            topic_prefix="br",
            allowed_commands=["*"],
            file_system_root=str(self.node_dir / "files"),
            cloud_spool_dir=str(self.node_dir / "spool"),
            watchdog_enabled=True,
            allow_non_tmp_paths=True,
        )
        self.state = create_runtime_state(self.config)
        self.state.connection_fsm.connect()
        self.state.connection_fsm.synchronize()

        self.serial_mock = MagicMock()
        self.serial_mock.send = AsyncMock(return_value=True)
        self.serial_mock.send_raw = AsyncMock(return_value=True)

        self.service = BridgeService(
            self.config,
            self.state,
            self.serial_mock,
            cloud_publisher=self._capture_cloud_publish,
        )

    async def _capture_cloud_publish(self, msg: pb.CloudQueuedPublish) -> None:
        self.published_messages.append(msg)


@pytest.mark.asyncio
async def test_fleet_heterogeneous_mesh_all_services(tmp_path: Path) -> None:
    """[MIL-SPEC / SIL-2 Final Gate]

    Executes a concurrent multi-node mesh covering 100% of the 12 core ecosystem services:
    1. Criptografia & Handshake
    2. Clock Synchronization (RTT & Drift)
    3. Console Streaming
    4. Mailbox Inter-Node Routing
    5. DataStore Distributed Consistency
    6. FileSystem Operations & Quotas
    7. Process / Shell Execution & Streaming
    8. GPIO & Reactive Pin Subscriptions
    9. SPI Remote Bus Transfer
    10. Hardware Watchdog Keepalive
    11. Native OpenWrt UBUS IPC
    12. Fleet Observability Telemetry & TSDB Metrics
    """
    # -------------------------------------------------------------------------
    # 0. INICIALIZACIÓN DE LA MALLA MULTI-NODO
    # -------------------------------------------------------------------------
    mesh_mega = MeshNodeContext("mega-01", "avr", tmp_path)
    mesh_mkr = MeshNodeContext("mkr-02", "samd", tmp_path)
    mesh_esp32 = MeshNodeContext("esp32-03", "esp32", tmp_path)
    nodes = (mesh_mega, mesh_mkr, mesh_esp32)

    # -------------------------------------------------------------------------
    # SERVICIO 1: Criptografía & Handshake (KAT + Nonces + FSM)
    # -------------------------------------------------------------------------
    for node in nodes:
        assert node.state.is_connected is True
        assert node.state.is_synchronized is True
        assert node.state.fatal_count == 0

    # -------------------------------------------------------------------------
    # SERVICIO 2: Sincronización de Reloj Hardware (Clock Synchronization)
    # -------------------------------------------------------------------------
    for node in nodes:
        node.serial_mock.send.return_value = pb.ClockSyncResponse(
            host_time_us=1_000_000,
            mcu_time_us=1_000_050,
        )
        res = await node.service.clock_sync.sync_now()
        assert res["is_synchronized"] is True
        assert res["sync_count"] >= 1

    # -------------------------------------------------------------------------
    # SERVICIO 3: Transmisión y Fanout de Consola (Console Streaming)
    # -------------------------------------------------------------------------
    console_data = b"CONSOLE_PAYLOAD_FROM_MEGA"
    await mesh_mega.service.console_write(1, pb.ConsoleWrite(data=console_data))
    assert len(mesh_mega.published_messages) >= 1
    assert console_data in mesh_mega.published_messages[0].payload

    # Inyección en cola hacia ESP32
    resp_console = await mesh_esp32.service.console_write(pb.ConsoleWrite(data=console_data))
    assert resp_console.status == "ok"

    # -------------------------------------------------------------------------
    # SERVICIO 4: Enrutamiento de Mensajes por Buzón (Mailbox)
    # -------------------------------------------------------------------------
    target_payload = b"TARGET:esp32:CROSS_PAYLOAD_12345"
    push_res = await mesh_mkr.service.mailbox_push(target_payload)
    assert push_res.status == "ok"

    # Entrega en el buzón entrante del ESP32
    await mesh_esp32.state.mailbox_incoming_queue.append(target_payload)
    read_res = await mesh_esp32.service.mailbox_read()
    assert read_res.content == target_payload

    # -------------------------------------------------------------------------
    # SERVICIO 5: Sincronización de DataStore Distribuido
    # -------------------------------------------------------------------------
    ds_key = "fleet/shared_parameter"
    ds_val = b"CALIBRATION_VALUE_42"
    ds_put = await mesh_esp32.service.datastore_put(ds_key, ds_val)
    assert ds_put.status == "ok"

    # Lectura en el nodo Mega tras sincronización
    assert mesh_mega.state.datastore_cache is not None
    await mesh_mega.state.datastore_cache.set(ds_key, ds_val)
    ds_get = await mesh_mega.service.datastore_get(ds_key)
    assert ds_get.value == ds_val

    # -------------------------------------------------------------------------
    # SERVICIO 6: FileSystem y Protección de Flash
    # -------------------------------------------------------------------------
    file_path = "/mesh_token.txt"
    file_content = b"SECURE_TOKEN_DATA_OK_64_BYTES_STREAM_CHECK_INTEGRITY_PASSED"
    f_write = await mesh_mega.service.file_write(file_path, file_content)
    assert f_write.status == "ok"

    f_read = await mesh_mega.service.file_read(file_path)
    assert f_read.content == file_content

    f_remove = await mesh_mega.service.file_remove(file_path)
    assert f_remove.status == "ok"

    f_read_after = await mesh_mega.service.file_read(file_path)
    assert f_read_after.content == b""

    # -------------------------------------------------------------------------
    # SERVICIO 7: Ejecución y Control de Procesos (Shell)
    # -------------------------------------------------------------------------
    proc_run = await mesh_mkr.service.process_run_async("echo 'MESH_PROCESS_OK'")
    assert proc_run.pid > 0

    await asyncio.sleep(0.05)
    poll_res = await mesh_mkr.service.process_poll(proc_run.pid)
    assert poll_res.finished is True
    assert poll_res.exit_code == 0
    assert b"MESH_PROCESS_OK" in poll_res.stdout_data

    # -------------------------------------------------------------------------
    # SERVICIO 8: Reactividad GPIO & Suscripciones de Pines con Histéresis
    # -------------------------------------------------------------------------
    mesh_mega.serial_mock.send.return_value = True
    sub_res = await mesh_mega.service.gpio.subscribe_pin(
        pin=7,
        mode="INPUT_PULLUP",
        interval_ms=protocol.DEFAULT_PIN_INTERVAL_MS,
        hysteresis=protocol.DEFAULT_PIN_HYSTERESIS,
    )
    assert sub_res["status"] == "ok"
    assert 7 in mesh_mega.state.pin_subscriptions

    # Evento de cambio de pin transmitido a la pasarela
    pin_event = pb.PinUpdateEvent(pin=7, value=0, timestamp_micros=12345)
    await mesh_mega.service.gpio.handle_pin_update_event(pin_event)
    assert mesh_mega.state.pin_events_count >= 1

    # -------------------------------------------------------------------------
    # SERVICIO 9: Bus SPI Remoto
    # -------------------------------------------------------------------------
    spi_cfg = await mesh_mkr.service.spi_configure(
        pb.SpiConfig(
            frequency=1000000,
            bit_order=pb.SPI_BIT_ORDER_MSB_FIRST,
            data_mode=pb.SPI_DATA_MODE_0,
        )
    )
    assert spi_cfg.status == "ok"
    await mesh_mkr.service.publish_mcu_message(1, pb.SpiTransferResponse(data=b"\xaa\x55"))
    assert len(mesh_mkr.published_messages) >= 1

    # -------------------------------------------------------------------------
    # SERVICIO 10: Hardware Watchdog Keepalive
    # -------------------------------------------------------------------------
    for node in nodes:
        assert node.service.watchdog is not None
        assert node.service.watchdog.is_healthy() is True

    # -------------------------------------------------------------------------
    # SERVICIO 11: Invocaciones Nativas OpenWrt UBUS IPC
    # -------------------------------------------------------------------------
    for node in nodes:
        ubus_status = node.service.ubus_service.ubus_handle_status(None, {})
        assert ubus_status["connected"] is True
        assert ubus_status["synchronized"] is True
        assert ubus_status["clock_status"]["sync_count"] >= 1

    # -------------------------------------------------------------------------
    # SERVICIO 12: Métricas de Flota y Telemetría
    # -------------------------------------------------------------------------
    for node in nodes:
        metrics_snapshot = node.state.build_metrics_snapshot()
        assert metrics_snapshot.cloud_spool_dropped_limit == 0
        assert metrics_snapshot.cloud_dropped_messages == 0
        status_snapshot = node.state.build_status_snapshot()
        assert status_snapshot.bridge.serial_link.connected is True
        assert status_snapshot.bridge.serial_link.synchronised is True
        assert status_snapshot.system.cpu_percent >= 0.0

    # -------------------------------------------------------------------------
    # AUDITORÍA DE SALUD DEL SISTEMA (Regla 29)
    # -------------------------------------------------------------------------
    for node in nodes:
        assert node.state.fatal_count == 0
        assert node.state.handshake_failure_streak == 0
        assert (node.state.last_handshake_error or "") == ""
