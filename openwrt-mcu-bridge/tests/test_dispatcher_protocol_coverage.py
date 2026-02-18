"""Coverage tests for dispatcher registrations against protocol spec.

These tests ensure that every MCU->Linux command defined in tools/protocol/spec.toml
has an installed handler (or is explicitly treated as a pre-sync exception).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from mcubridge.protocol.protocol import Command, Status
from mcubridge.router.routers import MCUHandlerRegistry, MQTTRouter
from mcubridge.services.dispatcher import _PRE_SYNC_ALLOWED_COMMANDS, BridgeDispatcher

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "tools" / "protocol" / "spec.toml"


class _DummyRuntime:
    async def send_frame(self, _command_id: int, _payload: bytes) -> bool:
        return True

    async def acknowledge_frame(self, *_args: object, **_kwargs: object) -> None:
        return None

    def is_link_synchronized(self) -> bool:
        return True

    def is_topic_action_allowed(self, _topic: object, _action: str) -> bool:
        return True

    async def reject_topic_action(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def publish_bridge_snapshot(self, *_args: object, **_kwargs: object) -> None:
        return None


class _DummyComponent:
    async def handle_xoff(self, _payload: bytes) -> bool:
        return True

    async def handle_xon(self, _payload: bytes) -> bool:
        return True

    async def handle_write(self, _payload: bytes) -> bool:
        return True

    async def handle_put(self, _payload: bytes) -> bool:
        return True

    async def handle_get_request(self, _payload: bytes) -> bool:
        return True

    async def handle_push(self, _payload: bytes) -> bool:
        return True

    async def handle_available(self, _payload: bytes) -> bool:
        return True

    async def handle_read(self, _payload: bytes) -> bool:
        return True

    async def handle_processed(self, _payload: bytes) -> bool:
        return True

    async def handle_remove(self, _payload: bytes) -> bool:
        return True

    async def handle_run(self, _payload: bytes) -> bool:
        return True

    async def handle_run_async(self, _payload: bytes) -> bool:
        return True

    async def handle_poll(self, _payload: bytes) -> bool:
        return True

    async def handle_digital_read_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_analog_read_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_unexpected_mcu_request(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def handle_get_free_memory_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_get_version_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_set_baudrate_resp(self, _payload: bytes) -> bool:
        return True


class _SystemHandlers:
    async def handle_link_sync_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_link_reset_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_get_capabilities_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_ack(self, _payload: bytes) -> None:
        return None

    async def handle_status(self, _payload: bytes) -> None:
        return None

    async def handle_process_kill(self, _payload: bytes) -> bool | None:
        return True


class _StatusHandlerFactory:
    def __init__(self, handler: _SystemHandlers) -> None:
        self._handler = handler

    def __call__(self, _status: Status):
        return self._handler.handle_status


def _load_mcu_to_linux_command_values() -> set[int]:
    raw = tomllib.loads(SPEC_PATH.read_text(encoding="utf-8"))
    values: set[int] = set()
    for entry in raw.get("commands", []):
        directions = entry.get("directions", [])
        if "mcu_to_linux" in directions:
            values.add(int(entry["value"]))
    return values


def _load_mcu_to_linux_ack_required_command_values() -> set[int]:
    raw = tomllib.loads(SPEC_PATH.read_text(encoding="utf-8"))
    values: set[int] = set()
    for entry in raw.get("commands", []):
        directions = entry.get("directions", [])
        if "mcu_to_linux" not in directions:
            continue
        if bool(entry.get("requires_ack", False)):
            values.add(int(entry["value"]))
    return values


def test_pre_sync_allowed_commands_are_only_link_responses() -> None:
    assert _PRE_SYNC_ALLOWED_COMMANDS == {
        Command.CMD_LINK_SYNC_RESP.value,
        Command.CMD_LINK_RESET_RESP.value,
    }


def test_all_mcu_to_linux_commands_have_registered_handlers() -> None:
    assert SPEC_PATH.exists(), f"Missing protocol spec at {SPEC_PATH}"

    registry = MCUHandlerRegistry()
    router = MQTTRouter()

    runtime = _DummyRuntime()
    from mcubridge.config.settings import get_default_config
    from mcubridge.state.context import create_runtime_state
    state = create_runtime_state(get_default_config())

    dispatcher = BridgeDispatcher(
        mcu_registry=registry,
        mqtt_router=router,
        state=state,
        send_frame=runtime.send_frame,
        acknowledge_frame=runtime.acknowledge_frame,
        is_topic_action_allowed=runtime.is_topic_action_allowed,
        reject_topic_action=runtime.reject_topic_action,
        publish_bridge_snapshot=runtime.publish_bridge_snapshot,
    )

    comp = _DummyComponent()
    dispatcher.register_components(
        console=comp,
        datastore=comp,
        file=comp,
        mailbox=comp,
        pin=comp,
        process=comp,
        shell=comp,
        system=comp,
    )

    sys_handlers = _SystemHandlers()
    dispatcher.register_system_handlers(
        handle_link_sync_resp=sys_handlers.handle_link_sync_resp,
        handle_link_reset_resp=sys_handlers.handle_link_reset_resp,
        handle_get_capabilities_resp=sys_handlers.handle_get_capabilities_resp,
        handle_ack=sys_handlers.handle_ack,
        status_handler_factory=_StatusHandlerFactory(sys_handlers),
        handle_process_kill=sys_handlers.handle_process_kill,
    )

    # All status codes must be registered.
    for status in Status:
        assert registry.get(status.value) is not None

    # All MCU->Linux commands must be registered.
    missing: list[int] = []
    for value in sorted(_load_mcu_to_linux_command_values()):
        if registry.get(value) is None:
            missing.append(value)

    assert not missing, "Missing MCU handlers for command ids: " + ", ".join(f"0x{value:02X}" for value in missing)


def test_ack_required_mcu_to_linux_commands_are_registered() -> None:
    registry = MCUHandlerRegistry()
    router = MQTTRouter()

    runtime = _DummyRuntime()
    from mcubridge.config.settings import get_default_config
    from mcubridge.state.context import create_runtime_state
    state = create_runtime_state(get_default_config())

    dispatcher = BridgeDispatcher(
        mcu_registry=registry,
        mqtt_router=router,
        state=state,
        send_frame=runtime.send_frame,
        acknowledge_frame=runtime.acknowledge_frame,
        is_topic_action_allowed=runtime.is_topic_action_allowed,
        reject_topic_action=runtime.reject_topic_action,
        publish_bridge_snapshot=runtime.publish_bridge_snapshot,
    )

    comp = _DummyComponent()
    dispatcher.register_components(
        console=comp,
        datastore=comp,
        file=comp,
        mailbox=comp,
        pin=comp,
        process=comp,
        shell=comp,
        system=comp,
    )

    sys_handlers = _SystemHandlers()
    dispatcher.register_system_handlers(
        handle_link_sync_resp=sys_handlers.handle_link_sync_resp,
        handle_link_reset_resp=sys_handlers.handle_link_reset_resp,
        handle_get_capabilities_resp=sys_handlers.handle_get_capabilities_resp,
        handle_ack=sys_handlers.handle_ack,
        status_handler_factory=_StatusHandlerFactory(sys_handlers),
        handle_process_kill=sys_handlers.handle_process_kill,
    )

    ack_required = sorted(_load_mcu_to_linux_ack_required_command_values())
    assert ack_required, "Expected at least one MCU->Linux command to require ACK"

    missing: list[int] = []
    for value in ack_required:
        if registry.get(value) is None:
            missing.append(value)

    assert not missing, "Missing MCU handlers for ACK-required command ids: " + ", ".join(
        f"0x{value:02X}" for value in missing
    )
