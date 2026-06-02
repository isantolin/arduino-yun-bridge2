"""Protocol package for MCU Bridge."""

from __future__ import annotations

def is_system_command(command_id: int) -> bool:
    """Check if a command ID is a system or status command (Exempt from security/RLE)."""
    from . import protocol
    raw_cmd = command_id & ~protocol.CMD_FLAG_COMPRESSED & protocol.UINT16_MAX
    return (protocol.STATUS_CODE_MIN <= raw_cmd <= protocol.STATUS_CODE_MAX) or \
           (protocol.SYSTEM_COMMAND_MIN <= raw_cmd <= protocol.SYSTEM_COMMAND_MAX)
