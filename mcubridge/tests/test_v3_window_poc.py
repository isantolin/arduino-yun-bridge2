import pytest
import asyncio
from mcubridge.protocol.v3.sliding_window import V3SlidingWindow

@pytest.mark.asyncio
async def test_v3_sliding_window_cumulative_ack():
    window = V3SlidingWindow(window_size=3)
    
    # Send up to window limit
    assert await window.send_frame(b"Frame 0") == True
    assert await window.send_frame(b"Frame 1") == True
    assert await window.send_frame(b"Frame 2") == True
    
    # Window should be full, 4th frame must be rejected
    assert await window.send_frame(b"Frame 3") == False
    assert window.pending_count() == 3
    
    # Cumulative ACK for Sequence 1 (Clears 0 and 1)
    await window.receive_ack(1)
    assert window.pending_count() == 1
    
    # We can now send 2 more frames (Seq 3 and Seq 4)
    assert await window.send_frame(b"Frame 3") == True
    assert await window.send_frame(b"Frame 4") == True
    
    # Simulate a drop and cumulative ACK jumping to 4 (Clears 2, 3, 4)
    await window.receive_ack(4)
    assert window.pending_count() == 0

@pytest.mark.asyncio
async def test_v3_sliding_window_wraparound():
    window = V3SlidingWindow(window_size=15)
    
    # Force seq near wrap-around point
    window.next_seq_to_send = 14
    
    await window.send_frame(b"A") # seq 14
    await window.send_frame(b"B") # seq 15
    await window.send_frame(b"C") # seq 0
    await window.send_frame(b"D") # seq 1
    
    assert window.pending_count() == 4
    
    # Ack wrapping around from 15 to 0
    await window.receive_ack(0)
    
    # Should clear 14, 15, and 0. Only 1 remains.
    assert window.pending_count() == 1
    assert 1 in window.unacked_frames