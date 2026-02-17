"""Comprehensive coverage completion tests."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge import metrics
from mcubridge.config.settings import RuntimeConfig
from mcubridge.daemon import BridgeDaemon
from mcubridge.protocol import protocol, topics
from mcubridge.services.file import FileComponent
from mcubridge.services.runtime import BridgeService
from mcubridge.util import mqtt_helper


def test_mqtt_helper_tls_edge_cases(tmp_path):
    config = MagicMock(spec=RuntimeConfig)
    config.tls_enabled = False
    assert mqtt_helper.configure_tls_context(config) is None

    config.tls_enabled = True
    config.mqtt_cafile = None
    config.mqtt_tls_insecure = True
    config.mqtt_certfile = None
    config.mqtt_keyfile = None
    with patch("ssl.create_default_context") :
        ctx = mqtt_helper.configure_tls_context(config)
        assert ctx.check_hostname is False

    config.mqtt_certfile = "cert.pem"
    config.mqtt_keyfile = None
    with pytest.raises(RuntimeError, match="Both mqtt_certfile and mqtt_keyfile"):
        mqtt_helper.configure_tls_context(config)


def test_topics_edge_cases():
    prefix = "br"
    with pytest.raises(ValueError, match="topic segment cannot be empty"):
        topics.topic_path(prefix, "")
    assert topics.parse_topic("wrong", "br/system/handshake") is None
    assert topics.parse_topic("br", "br") is None
    assert topics.parse_topic("br", "br/unknown/action") is None


def test_protocol_constants():
    assert protocol.PROTOCOL_VERSION == 2
    assert protocol.MIN_FRAME_SIZE > 0


@pytest.mark.asyncio
async def test_file_component_edge_cases(runtime_state, real_config):
    ctx = MagicMock()
    comp = FileComponent(real_config, runtime_state, ctx)
    assert comp._normalise_filename("file\x00name") is None
    assert comp._normalise_filename("") is None
    assert comp._normalise_filename("a/../b") is None
    assert comp._normalise_filename("/abc").parts == ("abc",)
    runtime_state.file_system_root = "/tmp/mcubridge"
    with patch("pathlib.Path.resolve", return_value=Path("/etc/passwd")):
        assert comp._get_safe_path("passwd") is None
    with patch("pathlib.Path.mkdir", side_effect=OSError("Permission denied")):
        assert comp._get_base_dir() is None
    runtime_state.allow_non_tmp_paths = False
    runtime_state.file_system_root = "/home/user"
    assert comp._get_base_dir() is None
    runtime_state.file_write_max_bytes = 10
    res = await comp._write_with_quota(Path("/tmp/f"), b"a" * 20)
    assert res[0] is False
    assert res[2] == "write_limit_exceeded"


@pytest.mark.asyncio
async def test_runtime_service_edge_cases(real_config, runtime_state):
    service = BridgeService(real_config, runtime_state)
    msg = MagicMock()
    msg.topic = "br/d/13/write"
    msg.payload = b"invalid"
    with patch.object(service._dispatcher, "dispatch_mqtt_message", side_effect=ValueError("Boom")):
        await service.handle_mqtt_message(msg)


@pytest.mark.asyncio
async def test_daemon_supervision_logic(real_config):
    daemon = BridgeDaemon(real_config)
    spec = MagicMock()
    spec.name = "test_task"
    spec.fatal_exceptions = (RuntimeError,)
    spec.max_restarts = 1
    spec.min_backoff = 0.01
    spec.max_backoff = 0.02
    spec.restart_interval = 0.01
    spec.factory = AsyncMock(return_value=None)
    await daemon._supervise_task(spec)
    spec.factory = AsyncMock(side_effect=RuntimeError("Fatal"))
    with pytest.raises(RuntimeError):
        await daemon._supervise_task(spec)


@pytest.mark.asyncio
async def test_metrics_emit_errors(runtime_state):
    enqueue = AsyncMock()
    with patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=TypeError("Fail")):
        with patch("asyncio.sleep", side_effect=asyncio.CancelledError()):
            with pytest.raises(asyncio.CancelledError):
                await metrics._bridge_snapshot_loop(runtime_state, enqueue, flavor="summary", seconds=1)

    with patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=AttributeError("Fatal")):
        with patch("asyncio.sleep", side_effect=asyncio.CancelledError()):
            with pytest.raises(asyncio.CancelledError):
                await metrics._bridge_snapshot_loop(runtime_state, enqueue, flavor="summary", seconds=1)


@pytest.mark.asyncio
async def test_prometheus_exporter_edge_cases(runtime_state):
    reg = MagicMock()
    exporter = metrics.PrometheusExporter(runtime_state, reg, port=0)
    await exporter.start()
    reader = AsyncMock()
    writer = AsyncMock()
    reader.readline = AsyncMock(return_value=b"")
    await exporter._handle_client(reader, writer)
    reader.readline = AsyncMock(side_effect=[b"GET /\r\n", b"\r\n"])
    await exporter._handle_client(reader, writer)
    reader.readline = AsyncMock(side_effect=[b"POST /metrics HTTP/1.1\r\n", b"\r\n"])
    await exporter._handle_client(reader, writer)
    reader.readline = AsyncMock(side_effect=RuntimeError("Unexpected"))
    await exporter._handle_client(reader, writer)
    await exporter.stop()


def test_metrics_collector_flatten(runtime_state):
    collector = metrics._RuntimeStateCollector(runtime_state)
    data = {"a": 1, "b": {"c": 2, "d": None}, "e": "str"}
    results = list(collector._flatten("test", data))
    assert ("gauge", "test_a", 1.0) in results
    assert ("gauge", "test_b_c", 2.0) in results


@pytest.mark.asyncio
async def test_daemon_supervision_base_exception(real_config):
    daemon = BridgeDaemon(real_config)
    spec = MagicMock()
    spec.name = "base_exc_task"
    spec.fatal_exceptions = (RuntimeError,)
    spec.max_restarts = 0
    spec.min_backoff = 0.01
    spec.max_backoff = 0.02
    spec.restart_interval = 1.0
    spec.factory = AsyncMock(side_effect=BaseException("Base"))
    with pytest.raises(BaseException):
        await daemon._supervise_task(spec)


@pytest.mark.asyncio
async def test_file_component_additional_gaps(runtime_state, real_config):
    ctx = MagicMock()
    comp = FileComponent(real_config, runtime_state, ctx)
    with patch("pathlib.PurePosixPath.is_absolute", return_value=True):
        with patch("pathlib.PurePosixPath.relative_to", side_effect=ValueError()):
            assert comp._normalise_filename("/absolute") is None
    with patch("mcubridge.services.file.scandir", side_effect=OSError("Scan fail")):
        assert comp._scan_directory_size(Path("/tmp")) == 0
    from mcubridge.services.file import _do_write_file
    with patch("pathlib.Path.open", side_effect=OSError("Write fail")):
        with pytest.raises(OSError):
            _do_write_file(Path("/tmp/fail"), b"data")


def test_metrics_more_gaps(runtime_state):
    from mcubridge.metrics import _normalize_interval
    assert _normalize_interval(-1, 10.0) is None
    assert _normalize_interval(5, 10.0) == 10
    from mcubridge.metrics import _sanitize_metric_name
    assert _sanitize_metric_name("test-metric.name") == "test_metric_name"


def test_mqtt_helper_apply_tls_settings():
    config = MagicMock(spec=RuntimeConfig)
    config.tls_enabled = True
    config.mqtt_cafile = "ca.pem"
    config.mqtt_certfile = "cert.pem"
    config.mqtt_keyfile = "key.pem"
    config.mqtt_tls_insecure = True
    client = MagicMock()
    mqtt_helper.apply_tls_to_paho(client, config)
    client.tls_set.assert_called_once()
    client.tls_insecure_set.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_daemon_supervisor_cancelled(real_config):
    daemon = BridgeDaemon(real_config)
    spec = MagicMock()
    spec.name = "cancel_task"
    spec.fatal_exceptions = (RuntimeError,)
    spec.restart_interval = 1.0
    spec.min_backoff = 0.1
    spec.max_backoff = 1.0
    spec.max_restarts = 1
    spec.factory = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await daemon._supervise_task(spec)


def test_daemon_main_exception_group(real_config):
    from mcubridge.daemon import main
    with patch("mcubridge.daemon.load_runtime_config", return_value=real_config):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
            with patch("mcubridge.daemon.BridgeDaemon.run", side_effect=ExceptionGroup("Group", [RuntimeError("Err")])):
                with pytest.raises(SystemExit) as cm:
                    main()
                assert cm.value.code == 1
