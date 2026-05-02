"""Verify that every command defined in protocol has a corresponding handler in BridgeDispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock

from mcubridge.protocol import protocol
from mcubridge.services.dispatcher import BridgeDispatcher
from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig
import warnings


def test_mcu_registry_completeness() -> None:
    """Verify that every command defined in protocol has a corresponding handler in BridgeDispatcher."""

    # Get all CMD_ constants from protocol module
    commands = {
        name: getattr(protocol.Command, name).value
        for name in dir(protocol.Command)
        if name.startswith("CMD_")
    }

    # Commands that are NOT handled by BridgeDispatcher (sent TO MCU or handled by Transport)
    excluded = {
        "CMD_SET_BAUDRATE",
        "CMD_SET_BAUDRATE_RESP",
        "CMD_SET_PIN_MODE",
        "CMD_DIGITAL_WRITE",
        "CMD_ANALOG_WRITE",
        "CMD_ENTER_BOOTLOADER",
        "CMD_GET_FREE_MEMORY",
        "CMD_GET_CAPABILITIES",
        "CMD_GET_VERSION",
        "CMD_SPI_BEGIN",
        "CMD_SPI_END",
        "CMD_SPI_SET_CONFIG",
        "CMD_SPI_TRANSFER",
        "CMD_LINK_SYNC",
        "CMD_LINK_RESET",
        "CMD_DATASTORE_GET_RESP",
        "CMD_MAILBOX_READ_RESP",
        "CMD_MAILBOX_AVAILABLE_RESP",
        "CMD_PROCESS_RUN_ASYNC_RESP",
        "CMD_PROCESS_POLL_RESP",
    }

    config = RuntimeConfig(
        serial_shared_secret=b"s_e_c_r_e_t_mock", allow_non_tmp_paths=True
    )
    state = create_runtime_state(config)

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=RuntimeWarning,
            message="coroutine '.*' was never awaited",
        )

        dispatcher = BridgeDispatcher(
            mcu_registry={},
            state=state,
            send_frame=AsyncMock(return_value=True),
            acknowledge_frame=AsyncMock(return_value=True),
            is_topic_action_allowed=lambda t, a: True,
            reject_topic_action=AsyncMock(return_value=True),
            publish_bridge_snapshot=AsyncMock(return_value=True),
        )

        dispatcher.register_components(
            console=AsyncMock(),
            datastore=AsyncMock(),
            file=AsyncMock(),
            mailbox=AsyncMock(),
            pin=AsyncMock(),
            process=AsyncMock(),
            spi=AsyncMock(),
            system=AsyncMock(),
        )
        # Register system handlers too
        dispatcher.register_system_handlers(
            handle_link_sync_resp=AsyncMock(return_value=True),
            handle_link_reset_resp=AsyncMock(return_value=True),
            handle_get_capabilities_resp=AsyncMock(return_value=True),
            handle_ack=AsyncMock(return_value=True),
            status_handler_factory=lambda status: AsyncMock(return_value=True),
            handle_process_kill=AsyncMock(return_value=True),
        )

    for name, cmd_id in commands.items():
        if name in excluded or name == "CMD_UNKNOWN":
            continue

        assert (
            cmd_id in dispatcher.mcu_registry
        ), f"BridgeDispatcher missing handler for {name} (ID: 0x{cmd_id:02X})"
