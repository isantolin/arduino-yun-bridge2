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
    async def poll_process(self, pid: int) -> pb.ProcessPollResponse: ...


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
            "datastore_get": {
                "call": self.ubus_handle_datastore_get,
                "args": {
                    "key": ubus.STRING,
                },
            },
            "mailbox_read": {
                "call": self.ubus_handle_mailbox_read,
                "args": {},
            },
            "file_write": {
                "call": self.ubus_handle_file_write,
                "args": {
                    "path": ubus.STRING,
                    "data": ubus.STRING,
                },
            },
            "process_run": {
                "call": self.ubus_handle_process_run,
                "args": {
                    "command": ubus.STRING,
                },
            },
            "process_kill": {
                "call": self.ubus_handle_process_kill,
                "args": {
                    "pid": ubus.INT32,
                },
            },
            "process_poll": {
                "call": self.ubus_handle_process_poll,
                "args": {
                    "pid": ubus.INT32,
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
                "watchdog": caps.watchdog,
                "eeprom": caps.eeprom,
                "dac": caps.dac,
                "hw_serial1": caps.hw_serial1,
                "fpu": caps.fpu,
                "logic_3v3": caps.logic_3v3,
                "big_buffer": caps.big_buffer,
                "i2c": caps.i2c,
                "spi": caps.spi,
                "sd": caps.sd,
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

    def ubus_handle_datastore_get(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.datastore_get'."""
        key = str(msg.get("key", ""))
        cache = self.runtime.state.datastore_cache
        if cache is None:
            return {"status": "error", "message": "Datastore cache unavailable"}
        val: bytes | None = self.run_sync(cache.get(key))
        if val is None:
            return {"status": "not_found", "key": key}
        try:
            val_str = val.decode("utf-8")
        except UnicodeDecodeError:
            val_str = f"<hex:{val.hex()}>"
        return {"status": "ok", "key": key, "value": val_str}

    def ubus_handle_mailbox_read(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.mailbox_read'."""
        try:
            item: bytes = self.run_sync(self.runtime.state.mailbox_incoming_queue.popleft())
        except IndexError:
            return {"status": "empty"}
        try:
            msg_str = item.decode("utf-8")
        except UnicodeDecodeError:
            msg_str = f"<hex:{item.hex()}>"
        return {"status": "ok", "message": msg_str}

    def ubus_handle_file_write(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.file_write'."""
        target_path = str(msg.get("path", ""))
        data_str = str(msg.get("data", ""))
        topic_name = f"{self.runtime.state.cloud_topic_prefix}/file/write/{target_path}"
        publish = pb.CloudQueuedPublish(
            topic_name=topic_name,
            payload=data_str.encode(),
        )
        self.schedule_async(self.runtime.handle_request(publish))
        return {"status": "ok", "path": target_path, "bytes_written": len(data_str)}

    def ubus_handle_process_run(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.process_run'."""
        command = str(msg.get("command", ""))
        pid = int(self.run_sync(self.runtime.run_process(command)))
        return {"status": "ok", "pid": pid}

    def ubus_handle_process_kill(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.process_kill'."""
        pid = int(msg.get("pid", 0))
        res: tuple[bool, str | None] = self.run_sync(self.runtime.kill_process(pid))
        success, err = res
        return {"status": "ok" if success else "error", "pid": pid, "error": err or ""}

    def ubus_handle_process_poll(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.process_poll'."""
        pid = int(msg.get("pid", 0))
        resp: pb.ProcessPollResponse = self.run_sync(self.runtime.poll_process(pid))

        try:
            out_str = resp.stdout_data.decode("utf-8")
        except UnicodeDecodeError:
            out_str = f"<hex:{resp.stdout_data.hex()}>"

        try:
            err_str = resp.stderr_data.decode("utf-8")
        except UnicodeDecodeError:
            err_str = f"<hex:{resp.stderr_data.hex()}>"

        return {
            "status": "ok" if resp.status == 0 else "error",
            "exit_code": resp.exit_code,
            "finished": resp.finished,
            "stdout": out_str,
            "stderr": err_str,
        }

    def run_sync(self, coro: Any) -> Any:
        """Execute a coroutine synchronously in a running or fresh event loop."""
        try:
            loop = asyncio.get_running_loop()
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=5.0)
        except RuntimeError:
            return asyncio.run(coro)

    def notify(self, event_type: str, data: dict[str, Any]) -> bool:
        """Broadcast a native UBUS event notification (e.g. 'mcubridge.sync')."""
        if self._conn is None or not self._is_active:
            return False
        try:
            self._conn.send(f"mcubridge.{event_type}", data)
            return True
        except (OSError, RuntimeError) as exc:
            logger.debug("Failed to send UBUS notification", event_name=event_type, error=str(exc))
            return False

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
