"""Unit and integration tests for Wave C interactive Pigweed console (pw_console + pw_metric)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import queue
import socket
import threading
from typing import Any, cast

import pw_cli.plugins
from pw_console.embed import PwConsoleEmbed
from pw_hdlc.decode import FrameDecoder
from pw_hdlc.encode import ui_frame
from pw_metric_proto import metric_service_pb2
from pw_rpc import ids as pw_ids, packets as pw_packets
from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket
from pw_status import Status
from pw_stream.stream_readers import CancellableReader
from pw_tokenizer.encode import encode_token_and_args
from pw_tokenizer.tokens import Database as TokenDatabase, TokenizedStringEntry, pw_tokenizer_65599_hash, write_binary
import pytest

from mcubridge.protocol import config_schema_pb2 as config_pb, mcu_wire_pb2 as pb, protocol
from mcubridge.security.security import LinkCipher, derive_link_keys
from mcubridge.services.handshake import SerialHandshakeManager
from tools.console import pw_bridge_console

_PW_METRIC_TOKEN_MASK = 0x0FFFFFFF


def _fixed32_id(value: int | None) -> int:
    """Return a fixed32 RPC identifier as an unsigned Python integer."""
    if value is None:
        raise AssertionError("RPC packet or descriptor omitted a fixed32 identifier")
    return value & 0xFFFFFFFF


class _QueueReader(CancellableReader):
    """In-memory CancellableReader feeding raw bytes into HdlcRpcClient."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        cast(Any, super()).__init__(self._queue)

    def feed(self, data: bytes) -> None:
        self._queue.put(data)

    def read(self) -> bytes:
        item = self._queue.get()
        return b"" if item is None else item

    def cancel_read(self) -> None:
        self._queue.put(None)


class _ListLogHandler(logging.Handler):
    """Thread-safe log handler recording formatted log lines with a wakeup event."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self._event = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))
        self._event.set()

    def wait_for_count(self, expected: int, timeout_s: float = 3.0) -> bool:
        deadline = timeout_s
        step = 0.02
        elapsed = 0.0
        while len(self.messages) < expected and elapsed < deadline:
            self._event.wait(step)
            self._event.clear()
            elapsed += step
        return len(self.messages) >= expected


def _isolated_logger(name: str) -> tuple[logging.Logger, _ListLogHandler]:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _ListLogHandler()
    logger.addHandler(handler)
    return logger, handler


def test_load_rpc_protos_exposes_all_bridge_and_metric_services() -> None:
    """Verify compiled protos expose all MCU Bridge services, pw_transfer, and pw_metric."""
    protos = pw_bridge_console.load_rpc_protos()
    service_names = set(pw_bridge_console.list_rpc_service_names(protos))
    expected_services = {
        "rpc.pb.SystemService",
        "rpc.pb.GpioService",
        "rpc.pb.ConsoleService",
        "rpc.pb.DataStoreService",
        "rpc.pb.MailboxService",
        "rpc.pb.FileSystemService",
        "rpc.pb.ProcessService",
        "rpc.pb.SpiService",
        "rpc.pb.ClockSyncService",
        "pw.transfer.Transfer",
        "pw.metric.proto.MetricService",
    }
    assert expected_services.issubset(service_names)

    reader = _QueueReader()
    sent_frames: list[bytes] = []
    detokenizer, _ = pw_bridge_console.build_detokenizer()
    mcu_logger, _ = _isolated_logger("test.pw_bridge_console.protos.mcu")
    console_logger, _ = _isolated_logger("test.pw_bridge_console.protos.console")
    client = pw_bridge_console.create_rpc_client(
        reader,
        sent_frames.append,
        detokenizer=detokenizer,
        mcu_logger=mcu_logger,
        console_logger=console_logger,
    )
    try:
        rpcs = client.rpcs(pw_bridge_console.DEFAULT_CHANNEL_ID)
        assert callable(rpcs.rpc.pb.SystemService.GetVersion)
        assert callable(rpcs.rpc.pb.GpioService.DigitalRead)
        assert callable(rpcs.rpc.pb.ConsoleService.Write)
        assert callable(rpcs.rpc.pb.DataStoreService.Get)
        assert callable(rpcs.rpc.pb.MailboxService.Push)
        assert callable(rpcs.rpc.pb.FileSystemService.Remove)
        assert callable(rpcs.rpc.pb.ProcessService.RunAsync)
        assert callable(rpcs.rpc.pb.SpiService.Transfer)
        assert callable(rpcs.rpc.pb.ClockSyncService.ClockSync)
        assert callable(rpcs.pw.transfer.Transfer.Read)
        assert callable(rpcs.pw.metric.proto.MetricService.Get)
    finally:
        client.close()


def test_token_database_resolution_and_detokenizer_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify CSV, firmware sibling, env var, and error paths for token databases."""
    monkeypatch.delenv("MCUBRIDGE_TOKEN_DATABASE", raising=False)
    token = pw_tokenizer_65599_hash("BRIDGE_SAFE_STATE")
    tokens_csv = tmp_path / "tokens.csv"
    tokens_csv.write_text(f'{token:08x},          ,"","BRIDGE_SAFE_STATE"\n', encoding="utf-8")

    # Explicit CSV path wins.
    dt_explicit, loaded_explicit = pw_bridge_console.build_detokenizer(tokens_csv)
    assert loaded_explicit == tokens_csv
    assert len(dt_explicit.database) == 1
    assert pw_bridge_console.decode_hdlc_log_frame(encode_token_and_args(token), dt_explicit) == "BRIDGE_SAFE_STATE"

    # Firmware sibling auto-discovery prefers tokens.csv next to firmware.elf.
    fw_elf = tmp_path / "firmware.elf"
    fw_elf.write_bytes(b"\x7fELF")
    dt_fw, loaded_fw = pw_bridge_console.build_detokenizer(firmware=fw_elf)
    assert loaded_fw == tokens_csv
    assert len(dt_fw.database) == 1

    # Environment variable fallback when explicit/firmware paths are omitted.
    monkeypatch.setenv("MCUBRIDGE_TOKEN_DATABASE", str(tokens_csv))
    dt_env, loaded_env = pw_bridge_console.build_detokenizer()
    assert loaded_env == tokens_csv
    assert len(dt_env.database) == 1

    # Missing env path degrades safely to empty detokenizer.
    monkeypatch.setenv("MCUBRIDGE_TOKEN_DATABASE", str(tmp_path / "missing.csv"))
    dt_missing_env, loaded_missing_env = pw_bridge_console.build_detokenizer()
    assert loaded_missing_env is None
    assert len(dt_missing_env.database) == 0

    # Explicit missing path raises FileNotFoundError.
    with pytest.raises(FileNotFoundError, match="Token database not found"):
        pw_bridge_console.build_detokenizer(tmp_path / "does_not_exist.csv")

    # Explicit invalid ELF/corrupt file raises ValueError.
    corrupt_csv = tmp_path / "corrupt.csv"
    corrupt_csv.write_bytes(b"\x00\xff\xfe\xfdnot-a-valid-db")
    with pytest.raises(ValueError, match="Failed to load token database"):
        pw_bridge_console.build_detokenizer(corrupt_csv)


def test_live_hdlc_log_and_console_frame_detokenization(tmp_path: Path) -> None:
    """Verify HDLC_ADDRESS_LOG detokenization and HDLC_ADDRESS_CONSOLE routing."""
    fmt = "MCU_STACK_WATERMARK arch=SAMD21 used=%u budget=%u"
    token = pw_tokenizer_65599_hash(fmt)
    tokens_csv = tmp_path / "tokens.csv"
    tokens_csv.write_text(f'{token:08x},          ,"","{fmt}"\n', encoding="utf-8")

    detokenizer, _ = pw_bridge_console.build_detokenizer(tokens_csv)
    mcu_logger, mcu_handler = _isolated_logger("test.pw_bridge_console.live.mcu")
    console_logger, console_handler = _isolated_logger("test.pw_bridge_console.live.console")

    reader = _QueueReader()
    client = pw_bridge_console.create_rpc_client(
        reader,
        lambda _data: None,
        detokenizer=detokenizer,
        mcu_logger=mcu_logger,
        console_logger=console_logger,
    )
    try:
        tokenized_payload = encode_token_and_args(token, 1536, 22240)
        reader.feed(ui_frame(protocol.HDLC_ADDRESS_LOG, tokenized_payload))
        reader.feed(ui_frame(protocol.HDLC_ADDRESS_LOG, b"plain diagnostic line\n"))
        reader.feed(ui_frame(protocol.HDLC_ADDRESS_LOG, b"\xff\xfe\xfd\xfc"))
        reader.feed(ui_frame(protocol.HDLC_ADDRESS_CONSOLE, b"hello console\r\n"))
        reader.feed(ui_frame(protocol.HDLC_ADDRESS_CONSOLE, b"\x80\x81"))

        assert mcu_handler.wait_for_count(3)
        assert console_handler.wait_for_count(2)
        assert mcu_handler.messages[0] == "MCU_STACK_WATERMARK arch=SAMD21 used=1536 budget=22240"
        assert mcu_handler.messages[1] == "plain diagnostic line"
        assert mcu_handler.messages[2].startswith("$")
        assert console_handler.messages[0] == "hello console"
        assert console_handler.messages[1] == "8081"
    finally:
        client.close()


def test_build_console_embed_and_metrics_roundtrip(tmp_path: Path) -> None:
    """Verify real PwConsoleEmbed configuration and live pw_metric MetricService.Get roundtrip."""
    group_tok = pw_tokenizer_65599_hash("bridge")
    wcet_tok = pw_tokenizer_65599_hash("wcet_max_us") & _PW_METRIC_TOKEN_MASK
    tx_tok = pw_tokenizer_65599_hash("tx_frames") & _PW_METRIC_TOKEN_MASK
    rx_tok = pw_tokenizer_65599_hash("rx_frames") & _PW_METRIC_TOKEN_MASK

    tokens_csv = tmp_path / "metrics_tokens.csv"
    tokens_csv.write_text(
        "\n".join(
            [
                f'{group_tok:08x},          ,"metrics","bridge"',
                f'{wcet_tok:08x},          ,"metrics","wcet_max_us"',
                f'{tx_tok:08x},          ,"metrics","tx_frames"',
                f'{rx_tok:08x},          ,"metrics","rx_frames"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    detokenizer, loaded_db = pw_bridge_console.build_detokenizer(tokens_csv)
    mcu_logger, _ = _isolated_logger("test.pw_bridge_console.embed.mcu")
    console_logger, _ = _isolated_logger("test.pw_bridge_console.embed.console")
    host_logger, _ = _isolated_logger("test.pw_bridge_console.embed.host")

    reader = _QueueReader()

    def _mock_mcu_metric_responder(encoded_hdlc: bytes) -> None:
        # Decode outbound HDLC RPC request for MetricService.Get and feed back
        # real pw_rpc SERVER_STREAM + RESPONSE packets over HDLC.
        from pw_hdlc.decode import FrameDecoder

        decoder = FrameDecoder()
        for frame in decoder.process(encoded_hdlc):
            if not frame.ok() or frame.address != protocol.HDLC_ADDRESS_RPC:
                continue
            req = RpcPacket.FromString(frame.data)
            if req.service_id == pw_ids.calculate(
                "pw.metric.proto.MetricService"
            ) and req.method_id == pw_ids.calculate("Get"):
                metric_resp = metric_service_pb2.MetricResponse(
                    metrics=[
                        metric_service_pb2.Metric(token_path=[group_tok, wcet_tok], as_int=412),
                        metric_service_pb2.Metric(token_path=[group_tok, tx_tok], as_int=19),
                        metric_service_pb2.Metric(token_path=[group_tok, rx_tok], as_int=23),
                    ]
                )
                stream_pkt = RpcPacket(
                    type=PacketType.SERVER_STREAM,
                    channel_id=req.channel_id,
                    service_id=req.service_id,
                    method_id=req.method_id,
                    call_id=req.call_id,
                    payload=metric_resp.SerializeToString(),
                )
                finish_pkt = RpcPacket(
                    type=PacketType.RESPONSE,
                    channel_id=req.channel_id,
                    service_id=req.service_id,
                    method_id=req.method_id,
                    call_id=req.call_id,
                    status=Status.OK.value,
                )
                reader.feed(ui_frame(protocol.HDLC_ADDRESS_RPC, stream_pkt.SerializeToString()))
                reader.feed(ui_frame(protocol.HDLC_ADDRESS_RPC, finish_pkt.SerializeToString()))

    client = pw_bridge_console.create_rpc_client(
        reader,
        _mock_mcu_metric_responder,
        detokenizer=detokenizer,
        mcu_logger=mcu_logger,
        console_logger=console_logger,
    )
    try:
        embed = pw_bridge_console.build_console_embed(
            client,
            detokenizer,
            mcu_logger=mcu_logger,
            console_logger=console_logger,
            host_logger=host_logger,
            endpoint_label="/dev/pts/42",
            token_db_path=loaded_db,
        )
        assert isinstance(embed, PwConsoleEmbed)
        assert embed.app_title == f"{pw_bridge_console.APP_TITLE} (/dev/pts/42)"
        embed_any: Any = embed
        embed_loggers: dict[str, list[logging.Logger]] = embed_any.loggers
        assert isinstance(embed_loggers, dict)
        assert set(embed_loggers.keys()) == {"MCU Logs", "MCU Console", "Host Logs"}
        assert embed_loggers["MCU Logs"] == [mcu_logger]
        embed_globals: dict[str, Any] = embed_any.global_vars
        assert isinstance(embed_globals, dict)
        for helper_key in ("device", "rpcs", "protos", "detokenizer", "get_metrics", "logs"):
            assert helper_key in embed_globals
        completers: list[Any] = embed_any.extra_completers
        assert len(completers) == 1
        assert embed.repl_startup_message is not None
        assert "get_metrics()" in embed.repl_startup_message

        # Exercise REPL helper end-to-end across HDLC + pw_rpc + pw_metric.metric_parser!
        metrics_tree: dict[str, Any] = embed_globals["get_metrics"](timeout_s=2.0)
        assert metrics_tree == {
            "bridge": {
                "wcet_max_us": 412,
                "tx_frames": 19,
                "rx_frames": 23,
            }
        }
    finally:
        client.close()


def test_pigweed_json_registers_bridge_console_plugin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify pigweed.json registers bridge-console cleanly for pw_cli and --check works."""
    repo_root = Path(__file__).resolve().parents[2]
    pigweed_json_path = repo_root / "pigweed.json"
    config: dict[str, Any] = json.loads(pigweed_json_path.read_text(encoding="utf-8"))

    registry = pw_cli.plugins.Registry(validator=pw_cli.plugins.callable_with_no_args)
    cast(Any, registry).register_config(config=config, path=pigweed_json_path)
    assert "bridge-console" in registry
    assert "bridge-console" not in registry.errors()

    tokens_csv = tmp_path / "tokens.csv"
    tok = pw_tokenizer_65599_hash("BRIDGE_FLOW_XOFF")
    tokens_csv.write_text(f'{tok:08x},          ,"","BRIDGE_FLOW_XOFF"\n', encoding="utf-8")

    exit_code = registry.run_with_argv(
        "bridge-console",
        ["--check", "--insecure", "--device", "tcp://127.0.0.1:9000", "--token-database", str(tokens_csv)],
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolved_url"] == "socket://127.0.0.1:9000"
    assert payload["link_security"] == "unauthenticated (explicit opt-in)"
    assert payload["token_database"] == str(tokens_csv)
    assert payload["token_entries"] == 1
    assert payload["hdlc_addresses"] == {
        "rpc": protocol.HDLC_ADDRESS_RPC,
        "log": protocol.HDLC_ADDRESS_LOG,
        "console": protocol.HDLC_ADDRESS_CONSOLE,
    }
    assert "rpc.pb.SystemService" in payload["services"]
    assert "pw.metric.proto.MetricService" in payload["services"]


def test_open_bridge_transport_supports_tcp_socket_and_loopback_serial() -> None:
    """Verify TCP socket (SocketReader) and serial URL (SerialReader) transports work end-to-end."""
    with pytest.raises(ValueError, match="must not be empty"):
        pw_bridge_console.open_bridge_transport("")

    with pytest.raises(ValueError, match="Invalid socket endpoint URL"):
        pw_bridge_console.open_bridge_transport("socket://:9000")

    # 1. Real ephemeral TCP socket link.
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    _, port = server_sock.getsockname()
    received_on_server: list[bytes] = []

    def _accept_and_echo() -> None:
        conn, _ = server_sock.accept()
        with conn:
            data = conn.recv(256)
            received_on_server.append(data)
            conn.sendall(data)

    server_thread = threading.Thread(target=_accept_and_echo, daemon=True)
    server_thread.start()
    try:
        transport = pw_bridge_console.open_bridge_transport(f"tcp://127.0.0.1:{port}", socket_timeout_s=2.0)
        try:
            assert transport.resolved_url == f"socket://127.0.0.1:{port}"
            frame = ui_frame(protocol.HDLC_ADDRESS_LOG, b"ping")
            transport.writer(frame)
            echoed = transport.reader.read()
            assert echoed == frame
        finally:
            transport.close()
        server_thread.join(timeout=2.0)
        assert received_on_server == [ui_frame(protocol.HDLC_ADDRESS_LOG, b"ping")]
    finally:
        server_sock.close()

    # 2. pyserial loopback URL.
    loop_transport = pw_bridge_console.open_bridge_transport("loop://", baudrate=115200)
    try:
        assert loop_transport.resolved_url == "loop://"
        sample = ui_frame(protocol.HDLC_ADDRESS_CONSOLE, b"loopback")
        loop_transport.writer(sample)
        assert loop_transport.reader.read() == sample
    finally:
        loop_transport.close()


def test_authenticated_console_handshake_and_bidirectional_frame_protection() -> None:
    """Verify the console performs LinkSync v2 and applies the daemon's AEAD cipher."""
    secret = b"console-test-shared-secret"
    mcu_nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    response_frames: queue.SimpleQueue[bytes] = queue.SimpleQueue()
    raw_reader = _QueueReader()
    wire_writes: list[bytes] = []
    link_sync_response_sent = False

    def _read_handshake_chunk(_size: int) -> bytes:
        try:
            return response_frames.get(timeout=1.0)
        except queue.Empty:
            return b""

    def _write_raw(encoded: bytes) -> None:
        nonlocal link_sync_response_sent
        wire_writes.append(encoded)
        if link_sync_response_sent:
            return
        decoder = FrameDecoder()
        for frame in decoder.process(encoded):
            if not frame.ok() or frame.address != protocol.HDLC_ADDRESS_RPC:
                continue
            packet = pw_packets.decode(frame.data)
            link_sync_spec = protocol.BY_NAME["rpc.pb.SystemService/LinkSync"]
            link_sync_ids = (_fixed32_id(link_sync_spec.service_id), _fixed32_id(link_sync_spec.method_id))
            if (
                _fixed32_id(packet.service_id),
                _fixed32_id(packet.method_id),
            ) != link_sync_ids or packet.type != PacketType.REQUEST:
                continue

            request = pb.LinkSync.FromString(packet.payload)
            response = pb.LinkSync(
                nonce=request.nonce,
                mcu_nonce=mcu_nonce,
                tag=SerialHandshakeManager.calculate_handshake_response_tag(secret, request.nonce, mcu_nonce),
            )
            response_packet = protocol.encode_rpc_packet(
                "rpc.pb.SystemService/LinkSync",
                response,
                call_id=packet.call_id,
                packet_type=int(PacketType.RESPONSE),
            )
            response_frames.put(ui_frame(protocol.HDLC_ADDRESS_RPC, response_packet))
            link_sync_response_sent = True

    transport = pw_bridge_console.BridgeTransport(
        endpoint="test",
        resolved_url="test://link",
        reader=raw_reader,
        writer=_write_raw,
        closer=lambda: None,
        read_chunk=_read_handshake_chunk,
        flush=lambda: None,
    )

    cipher, leftover = pw_bridge_console.establish_link_security(
        transport,
        secret,
        response_timeout_s=1.0,
        reset_settle_s=0.0,
    )
    assert cipher is not None
    assert leftover == b""
    assert len(wire_writes) == 2

    sync_request_frame = next(frame for frame in FrameDecoder().process(wire_writes[1]) if frame.ok())
    sync_packet = pw_packets.decode(sync_request_frame.data)
    sync_request = pb.LinkSync.FromString(sync_packet.payload)
    host_to_mcu, mcu_to_host = derive_link_keys(secret, sync_request.nonce, mcu_nonce)
    mcu_cipher = LinkCipher(mcu_to_host, host_to_mcu)

    secure_writer = pw_bridge_console.SecureHdlcWriter(transport, cipher)
    request_packet = protocol.encode_rpc_packet(
        "rpc.pb.SystemService/GetVersion",
        b"",
        call_id=17,
    )
    secure_writer(ui_frame(protocol.HDLC_ADDRESS_RPC, request_packet))
    encrypted_request = next(frame.data for frame in FrameDecoder().process(wire_writes[-1]) if frame.ok())
    assert mcu_cipher.decrypt(protocol.HDLC_ADDRESS_RPC, encrypted_request) == request_packet

    response_packet = protocol.encode_rpc_packet(
        "rpc.pb.SystemService/GetVersion",
        b"",
        call_id=17,
        packet_type=int(PacketType.RESPONSE),
    )
    raw_reader.feed(
        ui_frame(
            protocol.HDLC_ADDRESS_RPC,
            mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, response_packet),
        )
    )
    secure_reader = pw_bridge_console.SecureHdlcReader(raw_reader, cipher)
    assert secure_reader.read() == ui_frame(protocol.HDLC_ADDRESS_RPC, response_packet)

    reset_request = protocol.encode_rpc_packet(
        "rpc.pb.SystemService/LinkReset",
        b"",
        call_id=18,
    )
    writes_before_reset = len(wire_writes)
    with pytest.raises(ValueError, match="cannot be issued from the REPL"):
        secure_writer(ui_frame(protocol.HDLC_ADDRESS_RPC, reset_request))
    assert len(wire_writes) == writes_before_reset


def test_console_requires_explicit_authentication_or_insecure_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never silently start a plaintext console session when no secret is configured."""
    monkeypatch.delenv("MCUBRIDGE_SERIAL_SHARED_SECRET", raising=False)
    monkeypatch.setattr(pw_bridge_console, "get_config_source", lambda: "defaults")
    monkeypatch.setattr(pw_bridge_console, "load_runtime_config", config_pb.RuntimeConfig)
    with pytest.raises(ValueError, match="No serial shared secret is available"):
        pw_bridge_console.resolve_link_secret()
    assert pw_bridge_console.resolve_link_secret(insecure=True) == b""


def test_console_closes_transport_when_handshake_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the serial/socket even when LinkSync times out before RPC client creation."""
    reader = _QueueReader()
    closed = False

    def close_transport() -> None:
        nonlocal closed
        closed = True

    transport = pw_bridge_console.BridgeTransport(
        endpoint="test",
        resolved_url="test://link",
        reader=reader,
        writer=lambda _data: None,
        closer=close_transport,
        read_chunk=lambda _size: b"",
        flush=lambda: None,
    )

    def fake_resolve_link_secret(*, insecure: bool = False) -> bytes:
        del insecure
        return b"test-secret"

    def fake_open_bridge_transport(
        device: str,
        *,
        baudrate: int = protocol.DEFAULT_BAUDRATE,
        socket_timeout_s: float = pw_bridge_console.DEFAULT_SOCKET_TIMEOUT_S,
    ) -> pw_bridge_console.BridgeTransport:
        del device, baudrate, socket_timeout_s
        return transport

    monkeypatch.setattr(pw_bridge_console, "resolve_link_secret", fake_resolve_link_secret)
    monkeypatch.setattr(pw_bridge_console, "open_bridge_transport", fake_open_bridge_transport)

    def fail_handshake(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("simulated LinkSync timeout")

    monkeypatch.setattr(pw_bridge_console, "create_secure_rpc_io", fail_handshake)

    with pytest.raises(TimeoutError, match="simulated LinkSync timeout"):
        pw_bridge_console.run_console(device="test", insecure=False)

    assert closed


def test_binary_token_database_and_elf_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify binary Pigweed token DB loading and firmware ELF auto-discovery fallback."""
    monkeypatch.delenv("MCUBRIDGE_TOKEN_DATABASE", raising=False)
    token = pw_tokenizer_65599_hash("BRIDGE_FLOW_XON")
    bin_db_path = tmp_path / "tokens.bin"
    db = TokenDatabase([TokenizedStringEntry(token=token, string="BRIDGE_FLOW_XON")])
    with bin_db_path.open("wb") as handle:
        write_binary(db, handle)

    dt_bin, loaded_bin = pw_bridge_console.build_detokenizer(bin_db_path)
    assert loaded_bin == bin_db_path
    assert pw_bridge_console.decode_hdlc_log_frame(encode_token_and_args(token), dt_bin) == "BRIDGE_FLOW_XON"

    # When no sibling tokens.csv exists, resolve_token_database_path falls back to firmware.elf.
    elf_dir = tmp_path / "elf_only"
    elf_dir.mkdir()
    fw_bin = elf_dir / "firmware_flash.bin"
    fw_bin.write_bytes(b"\x00")
    fw_elf = elf_dir / "firmware.elf"
    fw_elf.write_bytes(b"\x7fELF")
    assert pw_bridge_console.resolve_token_database_path(firmware=fw_bin) == fw_elf
    # A truncated stub ELF degrades safely to an empty detokenizer during auto-discovery.
    dt_stub, loaded_stub = pw_bridge_console.build_detokenizer(firmware=fw_bin)
    assert loaded_stub is None
    assert len(dt_stub.database) == 0


def test_collect_metrics_tree_returns_empty_on_unimplemented_status() -> None:
    """Verify collect_metrics_tree handles UNIMPLEMENTED MetricService cleanly."""
    from pw_hdlc.decode import FrameDecoder

    reader = _QueueReader()

    def _respond_unimplemented(encoded_hdlc: bytes) -> None:
        decoder = FrameDecoder()
        for frame in decoder.process(encoded_hdlc):
            if not frame.ok() or frame.address != protocol.HDLC_ADDRESS_RPC:
                continue
            req = RpcPacket.FromString(frame.data)
            err_pkt = RpcPacket(
                type=PacketType.SERVER_ERROR,
                channel_id=req.channel_id,
                service_id=req.service_id,
                method_id=req.method_id,
                call_id=req.call_id,
                status=Status.UNIMPLEMENTED.value,
            )
            reader.feed(ui_frame(protocol.HDLC_ADDRESS_RPC, err_pkt.SerializeToString()))

    detokenizer, _ = pw_bridge_console.build_detokenizer()
    mcu_logger, _ = _isolated_logger("test.pw_bridge_console.unimpl.mcu")
    console_logger, _ = _isolated_logger("test.pw_bridge_console.unimpl.console")
    client = pw_bridge_console.create_rpc_client(
        reader,
        _respond_unimplemented,
        detokenizer=detokenizer,
        mcu_logger=mcu_logger,
        console_logger=console_logger,
    )
    try:
        metrics = pw_bridge_console.collect_metrics_tree(
            client.rpcs(pw_bridge_console.DEFAULT_CHANNEL_ID),
            detokenizer,
            timeout_s=2.0,
        )
        assert metrics == {}
    finally:
        client.close()


def test_main_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI entry point renders help and exits with code 0."""
    exit_code = pw_bridge_console.main(["--help"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "--device" in output
    assert "--token-database" in output
    assert "--check" in output


def test_gateway_metrics_cli_single_and_watch_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--gateway-metrics validates mTLS paths and emits JSON snapshots for one-shot and watch modes."""
    monkeypatch.delenv("MCUBRIDGE_CA_CERT", raising=False)
    monkeypatch.delenv("MCUBRIDGE_CLIENT_CERT", raising=False)
    monkeypatch.delenv("MCUBRIDGE_CLIENT_KEY", raising=False)
    assert pw_bridge_console.main(["--gateway-metrics"]) != 0

    ca = tmp_path / "ca.pem"
    cert = tmp_path / "client.pem"
    key = tmp_path / "client.key"
    for p in (ca, cert, key):
        p.write_text("PEM", encoding="utf-8")

    calls: list[tuple[str, int]] = []

    def _fake_fetch(host: str, port: int, **_kwargs: Any) -> dict[str, Any]:
        calls.append((host, port))
        return {"gateway": {"dispatches_total": len(calls)}}

    monkeypatch.setattr(pw_bridge_console, "fetch_gateway_metrics", _fake_fetch)

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(pw_bridge_console.time, "sleep", _no_sleep)

    rc = pw_bridge_console.main(
        [
            "--gateway-metrics",
            "--gateway-host",
            "127.0.0.1",
            "--gateway-port",
            "8443",
            "--cafile",
            str(ca),
            "--certfile",
            str(cert),
            "--keyfile",
            str(key),
            "--watch",
            "0.1",
            "--count",
            "2",
        ]
    )
    assert rc == 0
    assert calls == [("127.0.0.1", 8443), ("127.0.0.1", 8443)]
    out = capsys.readouterr().out
    assert '"dispatches_total": 1' in out
    assert '"dispatches_total": 2' in out


def test_fetch_gateway_metrics_closes_socket_and_uses_detokenizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_gateway_metrics detokenizes the MetricService tree and always closes the TLS socket."""
    closed: list[bool] = []

    class _FakeTlsSocket:
        def settimeout(self, _t: float) -> None:
            pass

        def recv(self, _n: int) -> bytes:
            return b""

        def sendall(self, _data: bytes) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    def _fake_open(*_args: Any, **_kwargs: Any) -> _FakeTlsSocket:
        return _FakeTlsSocket()

    def _fake_collect(_rpcs: Any, _detok: Any, *, timeout_s: float = 5.0) -> dict[str, Any]:
        assert timeout_s > 0
        return {"gateway": {"active_device_sessions": 2}}

    monkeypatch.setattr(pw_bridge_console, "open_gateway_tls_socket", _fake_open)
    monkeypatch.setattr(pw_bridge_console, "collect_metrics_tree", _fake_collect)

    res = pw_bridge_console.fetch_gateway_metrics(
        "127.0.0.1",
        8443,
        cafile=tmp_path / "ca.pem",
        certfile=tmp_path / "cert.pem",
        keyfile=tmp_path / "key.pem",
    )
    assert res == {"gateway": {"active_device_sessions": 2}}
    assert closed == [True]
