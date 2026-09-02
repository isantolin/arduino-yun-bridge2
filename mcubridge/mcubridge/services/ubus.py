"""Native OpenWrt UBUS RPC service integration for McuBridge. [SIL-2]

Provides direct IPC with OpenWrt ubusd, enabling LuCI-JS and system utilities
to query status, execute commands, and control MCU peripherals natively.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any, Protocol
import structlog

from ..config.settings import RuntimeConfig
from ..protocol import mcubridge_pb2 as pb
from ..state.context import RuntimeState

logger = structlog.get_logger("mcubridge.service.ubus")

try:
    ubus: Any = importlib.import_module("ubus")
except ImportError:
    ubus = None


class BridgeRuntimeFacade(Protocol):
    """Facade protocol decoupling UbusService from full BridgeService implementation."""

    config: RuntimeConfig
    state: RuntimeState

    async def handle_request(self, inbound: Any) -> None: ...
    async def run_process(self, command: str) -> int: ...
    async def kill_process(self, pid: int) -> tuple[bool, str | None]: ...


class UbusService:
    """Manages the lifecycle of McuBridge UBUS object registration on OpenWrt."""

    def __init__(self, runtime: BridgeRuntimeFacade) -> None:
        self.runtime = runtime
        self._conn: Any = None
        self._is_active = False

    @property
    def is_active(self) -> bool:
        """Return whether UBUS connection is active and registered."""
        return self._is_active

    @property
    def connection(self) -> Any:
        """Return the underlying active UBUS connection if connected."""
        return self._conn

    def start(self) -> bool:
        """Connect to ubusd and register the 'mcubridge' object."""
        if ubus is None:
            logger.debug("python-ubus module not available in this environment; skipping UBUS registration")
            return False

        try:
            self._conn = ubus.connect()
            self.register_methods()
            self._is_active = True
            logger.info("McuBridge registered successfully on OpenWrt UBUS ('mcubridge')")
            return True
        except (OSError, RuntimeError) as exc:
            logger.warning("Failed to connect to ubusd", error=str(exc))
            self._conn = None
            self._is_active = False
            return False

    def register_methods(self) -> None:
        """Register RPC methods on the active UBUS connection."""
        if self._conn is None or ubus is None:
            return

        methods: dict[str, Any] = {
            "status": {
                "call": self.ubus_handle_status,
                "args": {},
            },
            "digital_write": {
                "call": self.ubus_handle_digital_write,
                "args": {
                    "pin": ubus.INT32,
                    "value": ubus.INT32,
                },
            },
            "analog_write": {
                "call": self.ubus_handle_analog_write,
                "args": {
                    "pin": ubus.INT32,
                    "value": ubus.INT32,
                },
            },
            "mailbox_push": {
                "call": self.ubus_handle_mailbox_push,
                "args": {
                    "message": ubus.STRING,
                },
            },
            "datastore_set": {
                "call": self.ubus_handle_datastore_set,
                "args": {
                    "key": ubus.STRING,
                    "value": ubus.STRING,
                },
            },
        }
        self._conn.add("mcubridge", methods)

    def ubus_handle_status(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.status'."""
        state = self.runtime.state
        caps = state.mcu_capabilities
        version_str = (
            f"{state.mcu_version[0]}.{state.mcu_version[1]}.{state.mcu_version[2]}"
            if state.mcu_version is not None
            else "unknown"
        )

        caps_dict: dict[str, bool] = {
            "watchdog": False,
            "eeprom": False,
            "dac": False,
            "hw_serial1": False,
            "fpu": False,
            "logic_3v3": False,
            "big_buffer": False,
            "i2c": False,
            "spi": False,
            "sd": False,
        }
        if isinstance(caps, pb.Capabilities):
            caps_dict = {
                "watchdog": bool(caps.watchdog),
                "eeprom": bool(caps.eeprom),
                "dac": bool(caps.dac),
                "hw_serial1": bool(caps.hw_serial1),
                "fpu": bool(caps.fpu),
                "logic_3v3": bool(caps.logic_3v3),
                "big_buffer": bool(caps.big_buffer),
                "i2c": bool(caps.i2c),
                "spi": bool(caps.spi),
                "sd": bool(caps.sd),
            }
        elif isinstance(caps, dict):
            for k, v in caps.items():
                caps_dict[k] = bool(v)

        return {
            "connected": state.state in ("connected", "synchronized"),
            "synchronized": state.is_synchronized,
            "version": version_str,
            "capabilities": caps_dict,
        }

    def ubus_handle_digital_write(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.digital_write'."""
        pin = int(msg.get("pin", 0))
        val = int(msg.get("value", 0))
        topic_name = f"{self.runtime.state.cloud_topic_prefix}/digital/{pin}/set"
        publish = pb.CloudQueuedPublish(
            topic_name=topic_name,
            payload=str(val).encode(),
        )
        self.schedule_async(self.runtime.handle_request(publish))
        return {"status": "ok", "pin": pin, "value": val}

    def ubus_handle_analog_write(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.analog_write'."""
        pin = int(msg.get("pin", 0))
        val = int(msg.get("value", 0))
        topic_name = f"{self.runtime.state.cloud_topic_prefix}/analog/{pin}/set"
        publish = pb.CloudQueuedPublish(
            topic_name=topic_name,
            payload=str(val).encode(),
        )
        self.schedule_async(self.runtime.handle_request(publish))
        return {"status": "ok", "pin": pin, "value": val}

    def ubus_handle_mailbox_push(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.mailbox_push'."""
        message = str(msg.get("message", ""))
        topic_name = f"{self.runtime.state.cloud_topic_prefix}/mailbox/push"
        publish = pb.CloudQueuedPublish(
            topic_name=topic_name,
            payload=message.encode(),
        )
        self.schedule_async(self.runtime.handle_request(publish))
        return {"status": "ok", "message_length": len(message)}

    def ubus_handle_datastore_set(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.datastore_set'."""
        key = str(msg.get("key", ""))
        value = str(msg.get("value", ""))
        topic_name = f"{self.runtime.state.cloud_topic_prefix}/datastore/{key}/set"
        publish = pb.CloudQueuedPublish(
            topic_name=topic_name,
            payload=value.encode(),
        )
        self.schedule_async(self.runtime.handle_request(publish))
        return {"status": "ok", "key": key}

    def schedule_async(self, coro: Any) -> None:
        """Schedule a coroutine on the active running asyncio loop."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)

    def stop(self) -> None:
        """Disconnect from ubusd."""
        if self._conn is not None:
            try:
                self._conn.close()
            except (OSError, RuntimeError) as exc:
                logger.debug("Error during UBUS disconnect", error=str(exc))
            finally:
                self._conn = None
                self._is_active = False
