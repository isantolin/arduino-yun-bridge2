import pytest
import asyncio
from mcubridge.protocol.v3.router import V3VirtualRouter, V3Header, Endpoint

@pytest.mark.asyncio
async def test_v3_router_strict_priority():
    router = V3VirtualRouter()
    
    # 1. Enqueue messages in random order
    await router.route_incoming(V3Header(1, 0, Endpoint.BULK, 0), b"file chunk")
    await router.route_incoming(V3Header(1, 0, Endpoint.DATA, 1), b"mailbox")
    await router.route_incoming(V3Header(1, 0, Endpoint.SYS, 2), b"handshake")
    await router.route_incoming(V3Header(1, 0, Endpoint.CTRL, 3), b"gpio")
    
    # 2. Dequeue MUST strictly follow: SYS -> CTRL -> DATA -> BULK
    
    msg1 = await router.get_next_priority_message()
    assert msg1 is not None
    assert msg1[0].endpoint == Endpoint.SYS
    assert msg1[1] == b"handshake"
    
    msg2 = await router.get_next_priority_message()
    assert msg2[0].endpoint == Endpoint.CTRL
    assert msg2[1] == b"gpio"
    
    msg3 = await router.get_next_priority_message()
    assert msg3[0].endpoint == Endpoint.DATA
    assert msg3[1] == b"mailbox"
    
    msg4 = await router.get_next_priority_message()
    assert msg4[0].endpoint == Endpoint.BULK
    assert msg4[1] == b"file chunk"
    
    # 3. Empty state
    msg5 = await router.get_next_priority_message()
    assert msg5 is None

@pytest.mark.asyncio
async def test_v3_router_queue_full():
    router = V3VirtualRouter()
    
    # BULK queue maxsize is 5
    for i in range(5):
        success = await router.route_incoming(V3Header(1, 0, Endpoint.BULK, i), b"data")
        assert success is True
        
    # 6th should fail (non-blocking reject to save UART handler)
    success = await router.route_incoming(V3Header(1, 0, Endpoint.BULK, 6), b"drop")
    assert success is False