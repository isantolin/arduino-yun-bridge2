import pytest
import asyncio
from mcubridge.state.storage import SqliteDeque

@pytest.mark.asyncio
async def test_mailbox_static_queue_overflow() -> None:
    """Verify deterministic SIL-2 drop behavior (oldest first) when queue reaches its maxlen limit."""
    # Create static queue with limit 8 messages
    q = SqliteDeque(path=":memory:", maxlen=8)
    
    # Fill the queue up to limit (0..7)
    for i in range(8):
        await q.append(f"message_{i}".encode())
        
    assert await q.length() == 8
    
    # Push two more elements (8 and 9) causing overflow
    await q.append(b"message_8")
    await q.append(b"message_9")
    
    # Verify the limit is maintained
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
