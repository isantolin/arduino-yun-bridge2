"""Contract tests for the binary protocol and dispatcher behavior."""

from __future__ import annotations

import msgspec
import pytest
from unittest.mock import AsyncMock, MagicMock
from mcubridge.protocol import protocol, structures
from mcubridge.services.handshake import SerialHandshakeManager
from mcubridge.services.dispatcher import BridgeDispatcher


def test_protocol_constants_match_spec() -> None:
    # Basic sanity check on constants exported via generate.py
    assert protocol.MAX_PAYLOAD_SIZE > 0
    assert protocol.CRC_SIZE == 4
    assert protocol.UINT16_MAX == 65535


def test_handshake_config_binary_layout_matches_cpp_struct() -> None:
    # Validate encode/decode round-trip for HandshakeConfig payload
    # Using direct msgspec.msgpack (Zero Wrapper)
    sample = structures.HandshakeConfigPacket(ack_timeout_ms=750, ack_retry_limit=3, response_timeout_ms=120000)
    encoded = msgspec.msgpack.encode(sample)
    assert len(encoded) > 0
    decoded = msgspec.msgpack.decode(encoded, type=structures.HandshakeConfigPacket)
    assert decoded == sample


def test_handshake_tag_reference_vector_matches_spec() -> None:
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes, hmac

    secret = b"mcubridge-shared"
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))

    # [MIL-SPEC] Test must use HKDF derived key to match runtime implementation
    # Eradicated derive_handshake_key wrapper (Llamada directa a cryptography)
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=protocol.HANDSHAKE_HKDF_OUTPUT_LENGTH,
        salt=protocol.HANDSHAKE_HKDF_SALT,
        info=protocol.HANDSHAKE_HKDF_INFO_AUTH,
    )
    auth_key = hkdf.derive(secret)

    expected = hmac.HMAC(auth_key, hashes.SHA256())
    expected.update(nonce)
    expected_tag = expected.finalize()[: protocol.HANDSHAKE_TAG_LENGTH]

    # [SIL-2] Use static method directly for stateless calculation
    computed = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert computed == expected_tag


@pytest.mark.asyncio
async def test_mcu_registry_completeness() -> None:
    """Verify all protocol commands have a registered handler in dispatcher."""
    from mcubridge.state.context import create_runtime_state
    from tests._helpers import make_test_config
    import svcs
    import warnings

    config = make_test_config()
    state = create_runtime_state(config)

    # Identify all commands from the protocol module
    commands = {
        name: getattr(protocol.Command, name).value
        for name in dir(protocol.Command)
        if name.startswith("CMD_")
    }

    # Commands that are linux-only or don't need MCU side logic registration
    excluded = {
        "CMD_GET_VERSION_RESP",
        "CMD_GET_FREE_MEMORY_RESP",
        "CMD_LINK_SYNC_RESP",
        "CMD_LINK_RESET_RESP",
        "CMD_GET_CAPABILITIES_RESP",
        "CMD_SET_BAUDRATE_RESP",
        "CMD_ENTER_BOOTLOADER_RESP",
        "CMD_DIGITAL_READ_RESP",
        "CMD_ANALOG_READ_RESP",
        "CMD_DATASTORE_GET_RESP",
        "CMD_MAILBOX_READ_RESP",
        "CMD_MAILBOX_AVAILABLE_RESP",
        "CMD_FILE_READ_RESP",
        "CMD_PROCESS_RUN_ASYNC_RESP",
        "CMD_PROCESS_POLL_RESP",
        "CMD_SPI_TRANSFER_RESP",
        "CMD_XON",
        "CMD_XOFF",
        "CMD_SET_BAUDRATE",
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
    }

    reg = svcs.Registry()
    with warnings.catch_warnings():
        # [SIL-2] Suppress unawaited coroutine warnings for registration-only mocks
        warnings.filterwarnings("ignore", category=RuntimeWarning, message="coroutine '.*' was never awaited")

        for cls_name in [
            "ConsoleComponent",
            "DatastoreComponent",
            "FileComponent",
            "MailboxComponent",
            "PinComponent",
            "ProcessComponent",
            "SpiComponent",
            "SystemComponent",
        ]:
            cls = getattr(__import__("mcubridge.services", fromlist=[cls_name]), cls_name)
            # [SIL-2] Use standard AsyncMock with spec for high interface fidelity
            mock_inst = AsyncMock(spec=cls)
            reg.register_value(cls, mock_inst)  # type: ignore[reportUnknownMemberType]

        container = svcs.Container(reg)

        dispatcher = BridgeDispatcher(
            mcu_registry={},
            mqtt_router=AsyncMock(),
            state=state,
            send_frame=AsyncMock(return_value=True),
            acknowledge_frame=AsyncMock(return_value=True),
            is_topic_action_allowed=lambda t, a: True,
            reject_topic_action=AsyncMock(return_value=True),
            publish_bridge_snapshot=AsyncMock(return_value=True),
        )

        dispatcher.register_components(container)
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

        # Check if cmd_id exists in mcu_registry
        assert cmd_id in dispatcher.mcu_registry, f"BridgeDispatcher missing handler for {name} (ID: 0x{cmd_id:02X})"
