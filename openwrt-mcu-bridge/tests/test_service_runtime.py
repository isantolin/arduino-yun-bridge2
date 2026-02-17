"""Focused unit tests for BridgeService (runtime)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import msgspec
import pytest
from mcubridge.config.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=("echo", "ls"),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        console_queue_limit_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        serial_shared_secret=b"testshared",
    )


@pytest.mark.asyncio
async def test_send_frame_without_serial_sender_returns_false() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    ok = await service.send_frame(protocol.Command.CMD_GET_VERSION.value, b"x")
    assert ok is False


@pytest.mark.asyncio
async def test_schedule_background_requires_context() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    async def _coro() -> None:
        return None

    pending = _coro()
    try:
        with pytest.raises(RuntimeError):
            await service.schedule_background(pending)
    finally:
        pending.close()


@pytest.mark.asyncio
async def test_acknowledge_mcu_frame_no_sender_is_noop() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    await service._acknowledge_mcu_frame(protocol.Command.CMD_GET_VERSION.value, status=Status.ACK)


@pytest.mark.asyncio
async def test_acknowledge_mcu_frame_truncates_extra_payload() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    sent: list[tuple[int, bytes]] = []

    async def _sender(cmd: int, payload: bytes) -> bool:
        sent.append((cmd, payload))
        return True

    service.register_serial_sender(_sender)

    extra = b"x" * (protocol.MAX_PAYLOAD_SIZE * 2)
    await service._acknowledge_mcu_frame(
        protocol.Command.CMD_GET_FREE_MEMORY.value,
        status=Status.MALFORMED,
        extra=extra,
    )

    assert sent
    status_cmd, payload = sent[0]
    assert status_cmd == Status.MALFORMED.value
    assert payload.startswith(protocol.UINT16_STRUCT.build(protocol.Command.CMD_GET_FREE_MEMORY.value))
    assert len(payload) <= protocol.MAX_PAYLOAD_SIZE


@pytest.mark.asyncio
async def test_enqueue_mqtt_applies_reply_context_properties() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    msg = QueuedPublish(topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/x", payload=b"hello")

    props = SimpleNamespace(
        ResponseTopic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/resp",
        CorrelationData=b"cid",
    )
    inbound = SimpleNamespace(
        topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/origin",
        properties=props,
    )

    await service.enqueue_mqtt(msg, reply_context=inbound)  # type: ignore[arg-type]

    queued = state.mqtt_publish_queue.get_nowait()
    assert queued.topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/resp"
    assert queued.correlation_data == b"cid"
    assert (
        "bridge-request-topic",
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/origin",
    ) in queued.user_properties


@pytest.mark.asyncio
async def test_enqueue_mqtt_queue_full_drops_and_spools(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)

    # Create a tiny queue and fill it.
    state.mqtt_queue_limit = 1
    state.mqtt_publish_queue = asyncio.Queue(maxsize=1)

    # Avoid touching the real spool implementation (RuntimeState is slots=True,
    # so patch the class method rather than the instance attribute).
    async def _stash_ok(_self: RuntimeState, _message: QueuedPublish) -> bool:
        return True

    monkeypatch.setattr(RuntimeState, "stash_mqtt_message", _stash_ok)
    state.mqtt_spool = SimpleNamespace(pending=3)

    service = BridgeService(config, state)

    first = QueuedPublish(
        topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/old",
        payload=b"1",
    )
    state.mqtt_publish_queue.put_nowait(first)

    second = QueuedPublish(
        topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/new",
        payload=b"2",
    )
    await service.enqueue_mqtt(second)

    # Queue now contains the new message.
    queued = state.mqtt_publish_queue.get_nowait()
    assert queued.topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/new"

    # Drop counters updated.
    assert state.mqtt_dropped_messages == 1


@pytest.mark.asyncio
async def test_handle_get_free_memory_resp_malformed_no_publish() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    await service._system.handle_get_free_memory_resp(protocol.FRAME_DELIMITER)
    assert state.mqtt_publish_queue.qsize() == 0


@pytest.mark.asyncio
async def test_handle_get_version_resp_publishes_and_sets_state() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    await service._system.handle_get_version_resp(bytes([1, 2]))

    assert state.mcu_version == (1, 2)
    queued = state.mqtt_publish_queue.get_nowait()
    assert queued.payload == b"1.2"


@pytest.mark.asyncio
async def test_reject_topic_action_enqueues_status() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    inbound = SimpleNamespace(
        topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/secret",
        properties=None,
    )
    await service._reject_topic_action(inbound, Topic.SYSTEM, "reboot")  # type: ignore[arg-type]

    queued = state.mqtt_publish_queue.get_nowait()
    status_topic = topic_path(state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS)
    assert queued.topic_name == status_topic
    body = msgspec.json.decode(queued.payload)
    assert body["status"] == "forbidden"


@pytest.mark.asyncio
async def test_publish_bridge_snapshot_handshake_flavor() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    inbound = SimpleNamespace(
        topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/handshake/get",
        properties=None,
    )
    await service._publish_bridge_snapshot("handshake", inbound)  # type: ignore[arg-type]

    queued = state.mqtt_publish_queue.get_nowait()
    assert "bridge/handshake/value" in queued.topic_name


def test_is_topic_action_allowed_empty_action_true() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    assert service._is_topic_action_allowed(Topic.SYSTEM, "") is True


@pytest.mark.asyncio
async def test_enqueue_mqtt_spool_unavailable_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    state.mqtt_queue_limit = 1
    state.mqtt_publish_queue = asyncio.Queue(maxsize=1)

    async def _stash_fail(_self: RuntimeState, _message: QueuedPublish) -> bool:
        return False

    monkeypatch.setattr(RuntimeState, "stash_mqtt_message", _stash_fail)
    state.mqtt_spool_failure_reason = "disabled"
    state.mqtt_spool_backoff_until = time.monotonic() + 5

    service = BridgeService(config, state)
    state.mqtt_publish_queue.put_nowait(
        QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/old",
            payload=b"1",
        )
    )

    await service.enqueue_mqtt(
        QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/new",
            payload=b"2",
        )
    )

    queued = state.mqtt_publish_queue.get_nowait()
    assert queued.topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/new"
