import asyncio
from typing import Any
import pytest
import msgspec
from mcubridge.state import status
from mcubridge.state.context import RuntimeState


@pytest.mark.asyncio
async def test_status_writer_publishes_metrics(monkeypatch: Any, tmp_path: Any):
    status_path = tmp_path / "status.json"
    writes: list[dict[str, object]] = []

    def fake_write(payload: Any) -> None:
        data = msgspec.json.encode(payload)
        writes.append(msgspec.json.decode(data))
        status_path.write_bytes(data)

    monkeypatch.setattr(status, "STATUS_FILE", status_path)
    monkeypatch.setattr(status, "_write_status_file", fake_write)

    state = RuntimeState()
    state.mqtt_queue_limit = 42

    task = asyncio.create_task(status.status_writer(state, 0))
    for _ in range(10):
        if writes:
            break
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert writes, "status_writer no generó payload"
    payload = writes[0]

    # Just check some basics
    assert "bridge" in payload
    assert "process_stats" in payload
