import asyncio

import pytest
from prometheus_client import CONTENT_TYPE_LATEST

from yunbridge.metrics import PrometheusExporter
from yunbridge.state.context import SupervisorStats


@pytest.mark.asyncio
async def test_prometheus_exporter_serves_metrics(
    runtime_state, socket_enabled: None
):
    runtime_state.file_storage_quota_bytes = 4096
    runtime_state.file_storage_bytes_used = 1024
    runtime_state.supervisor_stats = {
        "worker": SupervisorStats(restarts=2),
    }

    exporter = PrometheusExporter(runtime_state, "127.0.0.1", 0)
    await exporter.start()
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", exporter.port
        )
        writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        payload = await reader.read()
        writer.close()
        await writer.wait_closed()
    finally:
        await exporter.stop()

    assert b"yunbridge_mqtt_queue_limit" in payload
    assert b"yunbridge_file_storage_quota_bytes" in payload
    assert b"yunbridge_supervisors_worker_restarts" in payload
    assert CONTENT_TYPE_LATEST.encode("ascii") in payload
