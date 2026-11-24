"""Native runtime metrics container for McuBridge edge daemon (SIL-2)."""

from __future__ import annotations

import collections
from dataclasses import dataclass, field


@dataclass(slots=True)
class DaemonMetrics:
    """Lightweight native metrics container backed by plain scalars and Counter maps."""

    cloud_messages_published: int = 0
    serial_bytes_sent: int = 0
    serial_bytes_received: int = 0
    serial_frames_sent: int = 0
    serial_frames_received: int = 0
    serial_retries: int = 0
    serial_failures: int = 0
    serial_crc_errors: int = 0
    serial_latency_ms: float = 0.0
    rpc_latency_ms: float = 0.0
    mcu_status_counts: collections.Counter[str] = field(default_factory=collections.Counter)
    uptime_seconds: float = 0.0
    link_state: str = "disconnected"
    handshake_state: str = "unsynchronized"
    retries: collections.Counter[str] = field(default_factory=collections.Counter)
