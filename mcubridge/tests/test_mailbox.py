import pytest
import tempfile
import os
from mcubridge.state.storage import SqliteDeque

@pytest.mark.asyncio
async def test_mailbox_static_queue_overflow() -> None:
    # Use a real file instead of :memory: because aiosqlite creates a new db per connection for :memory:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        path = tf.name
        
    try:
        q = SqliteDeque(path=path, maxlen=8)

        # Fill the queue up to limit (0..7)
        for i in range(8):
            await q.append(f"message_{i}".encode())

        assert await q.length() == 8

        # Push two more elements (8 and 9) causing overflow
        await q.append(b"message_8")
        await q.append(b"message_9")

        assert await q.length() == 8

        # Verify deterministic drop: oldest messages (message_0, message_1) must be dropped
        # resulting in message_2 as the first available
        msg = await q.popleft()
        assert msg == b"message_2"

        # Verify the remaining elements up to message_9
        for i in range(3, 10):
            assert await q.popleft() == f"message_{i}".encode()

        # Queue should be empty now
        assert await q.length() == 0
    finally:
        if os.path.exists(path):
            os.unlink(path)
