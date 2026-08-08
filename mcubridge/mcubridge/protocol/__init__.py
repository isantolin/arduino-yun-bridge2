def is_system_command(command_id: int) -> bool:
    """Check if a command ID is a system or status command (Exempt from security)."""
    from . import protocol  # Function-level to break circular import with config.common

    raw_cmd = command_id & protocol.UINT16_MAX
    return (protocol.STATUS_CODE_MIN <= raw_cmd <= protocol.STATUS_CODE_MAX) or (
        protocol.SYSTEM_COMMAND_MIN <= raw_cmd <= protocol.SYSTEM_COMMAND_MAX
    )
