from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import (
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_INTERVAL,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_DEFAULT_RESTART_INTERVAL,
)
from yunbridge.protocol import Command
from yunbridge.rpc import protocol
from yunbridge.services.components.datastore import DatastoreComponent
from yunbridge.state.context import RuntimeState


@pytest_asyncio.fixture
async def datastore_component() -> DatastoreComponent:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        serial_shared_secret=b"testsecret",
        serial_retry_timeout=DEFAULT_SERIAL_RETRY_TIMEOUT,
        serial_response_timeout=DEFAULT_SERIAL_RESPONSE_TIMEOUT,
        serial_retry_attempts=1,
        serial_handshake_min_interval=DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
        serial_handshake_fatal_failures=DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_tls=False,
        mqtt_cafile=DEFAULT_MQTT_CAFILE,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_topic=DEFAULT_MQTT_TOPIC,
        mqtt_spool_dir=DEFAULT_MQTT_SPOOL_DIR,
        mqtt_queue_limit=DEFAULT_MQTT_QUEUE_LIMIT,
        file_system_root=DEFAULT_FILE_SYSTEM_ROOT,
        file_write_max_bytes=DEFAULT_FILE_WRITE_MAX_BYTES,
        file_storage_quota_bytes=DEFAULT_FILE_STORAGE_QUOTA_BYTES,
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        process_max_output_bytes=DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
        process_max_concurrent=DEFAULT_PROCESS_MAX_CONCURRENT,
        console_queue_limit_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        pending_pin_request_limit=DEFAULT_PENDING_PIN_REQUESTS,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        bridge_summary_interval=DEFAULT_BRIDGE_SUMMARY_INTERVAL,
        bridge_handshake_interval=DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
        debug_logging=False,
        allowed_commands=(),
        metrics_enabled=False,
        metrics_host=DEFAULT_METRICS_HOST,
        metrics_port=DEFAULT_METRICS_PORT,
        watchdog_enabled=False,
        watchdog_interval=DEFAULT_WATCHDOG_INTERVAL,
        supervisor_restart_interval=SUPERVISOR_DEFAULT_RESTART_INTERVAL,
        supervisor_min_backoff=SUPERVISOR_DEFAULT_MIN_BACKOFF,
        supervisor_max_backoff=SUPERVISOR_DEFAULT_MAX_BACKOFF,
    )
    state = RuntimeState(config)
    component = DatastoreComponent(config, state)
    component.bind_mqtt_client(AsyncMock())
    component.bind_serial_writer(MagicMock())
    return component


@pytest.mark.asyncio
async def test_handle_put_success(
    datastore_component: DatastoreComponent,
) -> None:
    # Key length (1 byte) + Key + Content
    key = b"key"
    value = b"value"
    payload = bytes([len(key)]) + key + value

    await datastore_component.handle_put(payload)

    assert datastore_component.state.datastore.get("key") == "value"
    # Should publish to MQTT
    datastore_component._mqtt_client.publish.assert_called_once()  # pyright: ignore [reportOptionalMemberAccess]


@pytest.mark.asyncio
async def test_handle_put_malformed(
    datastore_component: DatastoreComponent,
) -> None:
    # Payload too short
    await datastore_component.handle_put(b"\x05key")
    # Should simply not crash/raise
    assert len(datastore_component.state.datastore) == 0


@pytest.mark.asyncio
async def test_handle_get_request_success(
    datastore_component: DatastoreComponent,
) -> None:
    datastore_component.state.datastore["key"] = "value"
    payload = b"key"

    await datastore_component.handle_get(payload)

    writer = datastore_component._serial_writer
    assert writer is not None
    writer.send_frame.assert_called_once()
    frame = writer.send_frame.call_args[0][0]
    assert frame.command_id == Command.CMD_DATASTORE_GET_RESP.value
    # Response format: Key len + Key + Value
    assert frame.payload == b"\x03keyvalue"


@pytest.mark.asyncio
async def test_handle_get_request_missing(
    datastore_component: DatastoreComponent,
) -> None:
    payload = b"missing"
    await datastore_component.handle_get(payload)

    writer = datastore_component._serial_writer
    assert writer is not None
    writer.send_frame.assert_called_once()
    frame = writer.send_frame.call_args[0][0]
    # Should return key + empty value
    assert frame.payload == b"\x07missing"
