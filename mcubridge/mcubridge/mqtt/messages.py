"""MQTT Message definitions.

Moved to mcubridge.protocol.structures as Single Source of Truth.
This module re-exports them for compatibility.
"""

from __future__ import annotations

from mcubridge.protocol.structures import QOSLevel, QueuedPublish, SpoolRecord

__all__ = ["QOSLevel", "QueuedPublish", "SpoolRecord"]
