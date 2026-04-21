import msgspec
from unittest.mock import MagicMock, AsyncMock
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

    computed = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert computed == expected_tag


def test_mcu_registry_completeness() -> None:
    """Verify that every command defined in protocol has a corresponding handler in BridgeDispatcher."""

    # Get all CMD_ constants from protocol module
    commands = {
        name: getattr(protocol.Command, name).value for name in dir(protocol.Command) if name.startswith("CMD_")
    }

    # Commands that are NOT handled by BridgeDispatcher (sent TO MCU or handled by Transport)
    excluded = {
        "CMD_SET_BAUDRATE",
        "CMD_SET_BAUDRATE_RESP",  # Handled by SerialTransport internally
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

    from mcubridge.state.context import create_runtime_state
    from tests._helpers import make_test_config
    import svcs

    config = make_test_config()
    state = create_runtime_state(config)

    # Mock components for registration
    class MockComp:
        def __getattr__(self, name):
            if name.startswith("handle_") or name == "on_serial_disconnected":
                return AsyncMock()
            raise AttributeError(name)

    reg = svcs.Registry()
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
        mock_inst = MockComp()
        mock_inst.__aenter__ = None
        mock_inst.__aexit__ = None
        reg.register_value(cls, mock_inst)

    container = svcs.Container(reg)

    dispatcher = BridgeDispatcher(
        mcu_registry={},
        mqtt_router=MagicMock(),
        state=state,
        send_frame=AsyncMock(),
        acknowledge_frame=AsyncMock(),
        is_topic_action_allowed=lambda t, a: True,
        reject_topic_action=AsyncMock(),
        publish_bridge_snapshot=AsyncMock(),
    )

    dispatcher.register_components(container)
    # Register system handlers too
    dispatcher.register_system_handlers(
        handle_link_sync_resp=AsyncMock(),
        handle_link_reset_resp=AsyncMock(),
        handle_get_capabilities_resp=AsyncMock(),
        handle_ack=AsyncMock(),
        status_handler_factory=lambda status: AsyncMock(),
        handle_process_kill=AsyncMock(),
    )

    for name, cmd_id in commands.items():
        if name in excluded or name == "CMD_UNKNOWN":
            continue

        # Check if cmd_id exists in mcu_registry
        assert cmd_id in dispatcher.mcu_registry, f"BridgeDispatcher missing handler for {name} (ID: 0x{cmd_id:02X})"
