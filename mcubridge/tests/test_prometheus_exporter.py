from typing import Any
import asyncio

import pytest
from mcubridge.metrics import PrometheusExporter
from mcubridge.state.context import SupervisorStats


@pytest.mark.asyncio
async def test_prometheus_exporter_serves_metrics(runtime_state: Any):
    runtime_state.file_storage_quota_bytes = 4096
    runtime_state.file_storage_bytes_used = 1024
    runtime_state.supervisor_stats = {
        "worker": SupervisorStats(restarts=2),
    }

    exporter = PrometheusExporter(runtime_state, "127.0.0.1", 0)
    # Start server in background
    task = asyncio.create_task(exporter.run())

    # Wait for the port to be non-zero (meaning server started)
    for _ in range(50):
        if exporter.port != 0:
            break
        await asyncio.sleep(0.1)

    port = exporter.port
    assert port != 0

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        payload = await reader.read()
        writer.close()
        await writer.wait_closed()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert b"mcubridge_queue_depth" in payload
    assert b"mcubridge_file_storage_bytes_used" in payload
    assert b"mcubridge_supervisor_worker_restarts" in payload
