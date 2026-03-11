"""Sliding Window Protocol Simulator for Bridge V3 PoC."""

import asyncio
import logging

logger = logging.getLogger(__name__)

class V3SlidingWindow:
    """Simulates the V3 sliding window on the MPU (Python) side."""

    def __init__(self, window_size: int = 15):
        self.window_size = window_size
        self.max_seq = 16
        self.unacked_frames = {}
        self.next_seq_to_send = 0
        self.expected_rx_seq = 0
        self.lock = asyncio.Lock()

    async def send_frame(self, payload: bytes) -> bool:
        async with self.lock:
            if len(self.unacked_frames) >= self.window_size:
                logger.warning("Sliding window full. Blocking.")
                return False

            seq = self.next_seq_to_send
            self.unacked_frames[seq] = payload
            self.next_seq_to_send = (self.next_seq_to_send + 1) % self.max_seq
            
            # Simulate sending to serial
            logger.debug(f"Sent Frame Seq={seq}, Payload={payload}")
            return True

    async def receive_ack(self, ack_seq: int):
        async with self.lock:
            # Cumulative ACK: Clear all frames up to ack_seq
            cleared = []
            for seq in list(self.unacked_frames.keys()):
                # Circular arithmetic check
                diff = (ack_seq - seq) % self.max_seq
                if diff < self.window_size:
                    cleared.append(seq)
                    del self.unacked_frames[seq]
            
            logger.debug(f"Received Cumulative ACK={ack_seq}. Cleared: {cleared}")

    def pending_count(self) -> int:
        return len(self.unacked_frames)
