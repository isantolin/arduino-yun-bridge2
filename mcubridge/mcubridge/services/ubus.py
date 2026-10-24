"""Native OpenWrt UBUS RPC service integration for McuBridge. [SIL-2]

Provides direct IPC with OpenWrt ubusd, enabling LuCI-JS and system utilities
to query status, execute commands, and control MCU peripherals natively.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Any, Protocol

import anyio.from_thread
import structlog
import tenacity
from google.protobuf.json_format import MessageToDict, ParseDict

from ..config.settings import RuntimeConfig
from ..protocol import mcubridge_pb2 as pb
from ..protocol.protocol import REGISTRY, Command
from ..state.context import RuntimeState

logger = structlog.get_logger("mcubridge.service.ubus")

try:
    ubus: Any = importlib.import_module("ubus")
except ImportError as exc:
    logger.debug("Native ubus module unavailable; fallback mode active", error=str(exc))
    ubus = None


def format_ubus_bytes(data: bytes) -> str:
    """Safely decode bytes to UTF-8 or return canonical hex string. [SIL-2]"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"<hex:{data.hex()}>"


def get_ubus_type(typ: str, ubus_module: Any = None) -> Any:
    """Resolve UBUS blobmsg type identifier safely."""
    target_ubus = ubus_module if ubus_module is not None else ubus
    if target_ubus is None:
        return 0
    return getattr(target_ubus, f"BLOBMSG_TYPE_{typ}", getattr(target_ubus, typ, 0))


class BridgeRuntimeFacade(Protocol):
    """Facade protocol decoupling UbusService from full BridgeService implementation."""

    config: RuntimeConfig
    state: RuntimeState

    async def handle_request(self, inbound: Any) -> None: ...

    async def run_process(self, command: str) -> int: ...

    async def kill_process(self, pid: int) -> tuple[bool, str | None]: ...

    async def poll_process(self, pid: int) -> pb.ProcessPollResponse: ...

    async def reset_link(self) -> bool: ...

    async def file_write(self, path: str, data: bytes) -> bool: ...

    async def datastore_put(
        self, _seq_or_key: int | str | pb.DatastorePut, value: bytes | None = None, *, reply_context: Any | None = None
    ) -> bool: ...

    async def mailbox_push(self, data: bytes) -> bool: ...

    clock_sync: Any

    gpio: Any


class UbusService:
    """Manages the lifecycle of McuBridge UBUS object registration on OpenWrt."""

    def __init__(
        self,
        runtime: BridgeRuntimeFacade,
        ubus_module: Any = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.runtime = runtime
        self.ubus_backend: Any = ubus_module if ubus_module is not None else ubus
        self.connection: Any = None
        self.is_active = False
        self.loop: asyncio.AbstractEventLoop | None = loop

    def start(
        self,
        max_attempts: int = 5,
        retry_wait: tenacity.wait.wait_base | None = None,
    ) -> bool:
        """Connect to ubusd and register the 'mcubridge' object with bounded backoff."""
        if self.ubus_backend is None:
            logger.debug("python-ubus module not available in this environment; skipping UBUS registration")
            return False

        def _connect() -> Any:
            if (conn := self.ubus_backend.connect()) is None:
                raise OSError("ubus.connect() returned None")
            return conn

        wait_strategy = (
            retry_wait if retry_wait is not None else tenacity.wait_exponential(multiplier=0.05, min=0.05, max=0.5)
        )
        retryer = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(max_attempts),
            wait=wait_strategy,
            retry=tenacity.retry_if_exception_type((OSError, RuntimeError)),
            reraise=True,
        )

        try:
            self.connection = retryer(_connect)
            self.register_methods()
            self.is_active = True
            logger.info("McuBridge registered successfully on OpenWrt UBUS ('mcubridge')")
            return True
        except (OSError, RuntimeError, tenacity.RetryError) as exc:
            logger.warning("Failed to connect to ubusd after retries", error=str(exc))
            self.connection = None
            self.is_active = False
            return False

    def register_methods(self) -> None:
        """Register RPC methods on the active UBUS connection."""
        if self.connection is None or self.ubus_backend is None:
            return

        def _make_handler(handler: Any) -> Any:
            def _cb(req: Any, msg: dict[str, Any]) -> None:
                try:
                    res = handler(req, msg)
                    if req and hasattr(req, "reply") and isinstance(res, dict):
                        req.reply(res)
                except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
                    logger.error("Exception in UBUS handler", handler=handler.__name__, error=str(exc))
                    raise

            return _cb

        session_type = get_ubus_type("STRING", self.ubus_backend)
        methods = REGISTRY.build_ubus_methods(
            self,
            self.ubus_backend,
            handler_factory=_make_handler,
            get_type_fn=get_ubus_type,
            session_type=session_type,
        )

        if hasattr(self.connection, "add") and callable(self.connection.add):
            self.connection.add("mcubridge", methods)
        elif hasattr(self.ubus_backend, "add") and callable(self.ubus_backend.add):
            self.ubus_backend.add("mcubridge", methods)

    async def run(self) -> None:
        """Background loop to process incoming OpenWrt UBUS events directly in asyncio."""
        if self.connection is None or self.ubus_backend is None or not hasattr(self.ubus_backend, "loop"):
            return
        logger.info("Starting OpenWrt UBUS event loop")
        try:
            while self.is_active:
                self.ubus_backend.loop(0)
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            logger.info("OpenWrt UBUS event loop cancelled")
            self.stop()
            raise

    def ubus_handle_status(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.status' returning holistic SIL-2 snapshot."""
        state = self.runtime.state
        snapshot = state.build_status_snapshot()
        data = MessageToDict(snapshot, preserving_proto_field_name=True)

        version_str = ".".join(map(str, state.mcu_version)) if state.mcu_version is not None else "unknown"
        data["connected"] = state.state in ("connected", "synchronized")
        data["synchronized"] = state.is_synchronized
        data["version"] = version_str

        # Ensure top-level capabilities dict exists for direct LuCI and tool consumers
        caps = state.mcu_capabilities
        data["capabilities"] = (
            MessageToDict(caps, always_print_fields_with_no_presence=True, preserving_proto_field_name=True)
            if isinstance(caps, pb.Capabilities)
            else {k: bool(v) for k, v in caps.items()} if isinstance(caps, dict) else {}
        )
        data["clock_status"] = {
            "offset_us": state.clock_offset_us,
            "rtt_us": state.clock_rtt_us,
            "sync_count": state.clock_sync_count,
            "last_sync_unix": state.clock_last_sync_timestamp,
        }
        data["pin_subscriptions"] = state.pin_subscriptions

        return data

    def _call_subsystem(
        self,
        service_attr: str,
        action: Callable[[Any], dict[str, Any]],
        error_message: str,
    ) -> dict[str, Any]:
        service = getattr(self.runtime, service_attr, None)
        if service:
            return action(service)
        return {"status": "error", "message": error_message}

    def ubus_handle_clock_status(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.clock_status'."""
        return self._call_subsystem(
            "clock_sync",
            lambda s: s.get_status(),
            "Clock service unavailable",
        )

    def ubus_handle_clock_sync(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.clock_sync'."""
        return self._call_subsystem(
            "clock_sync",
            lambda s: self.run_sync(s.sync_now()),
            "Clock service unavailable",
        )

    def ubus_handle_pin_subscribe(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.pin_subscribe'."""
        norm_msg = dict(msg)
        if "enabled" in norm_msg:
            norm_msg["enabled"] = bool(norm_msg["enabled"])
        proto = pb.PinSubscribeRequest()
        ParseDict(norm_msg, proto, ignore_unknown_fields=True)
        pin = proto.pin
        mode = proto.mode or str(msg.get("mode", "INPUT"))
        interval_ms = proto.interval_ms or int(msg.get("interval_ms", 50))
        hysteresis = proto.hysteresis or int(msg.get("hysteresis", 1))
        enabled = proto.enabled if "enabled" in msg else True
        return self._call_subsystem(
            "gpio",
            lambda s: self.run_sync(s.subscribe_pin(pin, mode, interval_ms, hysteresis, enabled)),
            "GPIO service unavailable",
        )

    def ubus_handle_link_reset(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.link_reset'."""
        ok = bool(self.run_sync(self.runtime.reset_link()))
        return {"status": "ok" if ok else "error"}

    def ubus_handle_ping(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.ping'."""
        is_synced = self.runtime.state.is_synchronized
        return {
            "status": "ok" if is_synced else "not_synchronized",
            "connected": self.runtime.state.state in ("connected", "synchronized"),
            "synchronized": is_synced,
        }

    def _handle_pin_write(self, kind: str, msg: dict[str, Any]) -> dict[str, Any]:
        proto = pb.DigitalWrite()
        ParseDict(msg, proto, ignore_unknown_fields=True)
        pin, val = proto.pin, proto.value
        cmd = Command.CMD_DIGITAL_WRITE if kind == "digital" else Command.CMD_ANALOG_WRITE
        payload = pb.DigitalWrite(pin=pin, value=val) if kind == "digital" else pb.AnalogWrite(pin=pin, value=val)
        if self.runtime.serial:
            self.schedule_async(self.runtime.serial.send(cmd.value, payload))
        return {"status": "ok", "pin": pin, "value": val}

    def ubus_handle_digital_write(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.digital_write'."""
        return self._handle_pin_write("digital", msg)

    def ubus_handle_analog_write(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.analog_write'."""
        return self._handle_pin_write("analog", msg)

    def ubus_handle_mailbox_push(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.mailbox_push'."""
        message = str(msg.get("message", ""))
        self.schedule_async(self.runtime.mailbox_push(message.encode("utf-8")))
        return {"status": "ok", "message_length": len(message)}

    def ubus_handle_datastore_set(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.datastore_set'."""
        key = str(msg.get("key", ""))
        value = str(msg.get("value", ""))
        self.schedule_async(self.runtime.datastore_put(key, value.encode("utf-8")))
        return {"status": "ok", "key": key}

    def ubus_handle_datastore_get(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.datastore_get'."""
        proto = pb.DatastoreGet()
        ParseDict(msg, proto, ignore_unknown_fields=True)
        key = proto.key
        cache = self.runtime.state.datastore_cache
        if cache is None:
            return {"status": "error", "message": "Datastore cache unavailable"}
        val: bytes | None = self.run_sync(cache.get(key))
        if val is None:
            return {"status": "not_found", "key": key}
        return {"status": "ok", "key": key, "value": format_ubus_bytes(val)}

    def ubus_handle_mailbox_read(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.mailbox_read'."""
        try:
            item: bytes = self.run_sync(self.runtime.state.mailbox_incoming_queue.popleft())
        except IndexError:
            logger.debug("UBUS mailbox read called on empty queue")
            return {"status": "empty"}
        return {"status": "ok", "message": format_ubus_bytes(item)}

    def ubus_handle_file_write(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.file_write'."""
        target_path = str(msg.get("path", ""))
        data_str = str(msg.get("data", ""))
        self.schedule_async(self.runtime.file_write(target_path, data_str.encode("utf-8")))
        return {"status": "ok", "path": target_path, "bytes_written": len(data_str)}

    def ubus_handle_process_run(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.process_run'."""
        proto = pb.ProcessRunAsync()
        ParseDict(msg, proto, ignore_unknown_fields=True)
        pid = int(self.run_sync(self.runtime.run_process(proto.command)))
        return {"status": "ok", "pid": pid}

    def ubus_handle_process_kill(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.process_kill'."""
        proto = pb.ProcessKill()
        ParseDict(msg, proto, ignore_unknown_fields=True)
        pid = proto.pid
        res: tuple[bool, str | None] = self.run_sync(self.runtime.kill_process(pid))
        success, err = res
        return {"status": "ok" if success else "error", "pid": pid, "error": err or ""}

    def ubus_handle_process_poll(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.process_poll'."""
        proto = pb.ProcessPoll()
        ParseDict(msg, proto, ignore_unknown_fields=True)
        pid = proto.pid
        resp: pb.ProcessPollResponse = self.run_sync(self.runtime.poll_process(pid))
        data = MessageToDict(resp, always_print_fields_with_no_presence=True, preserving_proto_field_name=True)
        data["status"] = "ok" if resp.status == 0 else "error"
        data["stdout"] = format_ubus_bytes(resp.stdout_data)
        data["stderr"] = format_ubus_bytes(resp.stderr_data)
        data.pop("stdout_data", None)
        data.pop("stderr_data", None)
        return data

    def run_sync(self, coro: Any) -> Any:
        """Execute a coroutine synchronously in a running or fresh event loop. [SIL-2]"""
        target_loop = self.loop
        if target_loop is not None and target_loop.is_running():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                logger.debug("No active running loop in current thread", error=str(exc))
                current_loop = None

            if current_loop is not target_loop:
                fut = asyncio.run_coroutine_threadsafe(coro, target_loop)
                return fut.result(timeout=5.0)

        try:
            return anyio.from_thread.run(lambda: coro)
        except (anyio.NoEventLoopError, RuntimeError) as exc:
            logger.debug("anyio from_thread run failed; falling back to thread pool or loop", error=str(exc))
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as loop_exc:
                logger.debug("No active running loop in thread fallback", error=str(loop_exc))
                loop = None

            if loop is not None and loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result(timeout=5.0)
            return asyncio.run(coro)

    def notify(self, event_type: str, data: dict[str, Any]) -> bool:
        """Broadcast a native UBUS event notification (e.g. 'mcubridge.sync')."""
        if self.connection is None or not self.is_active:
            return False
        try:
            if hasattr(self.connection, "send") and callable(self.connection.send):
                self.connection.send(f"mcubridge.{event_type}", data)
            elif (
                self.ubus_backend is not None
                and hasattr(self.ubus_backend, "send")
                and callable(self.ubus_backend.send)
            ):
                self.ubus_backend.send(f"mcubridge.{event_type}", data)
            return True
        except (OSError, RuntimeError) as exc:
            logger.debug("Failed to send UBUS notification", event_name=event_type, error=str(exc))
            return False

    def schedule_async(self, coro: Any) -> None:
        """Schedule a coroutine on the active running asyncio loop."""
        try:
            current_loop = asyncio.get_running_loop()
            current_loop.create_task(coro)
        except RuntimeError:
            loop = self.loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(coro, loop)
                return
            asyncio.run(coro)

    def stop(self) -> None:
        """Disconnect from ubusd."""
        self.is_active = False
        if self.connection is not None:
            try:
                if hasattr(self.connection, "close") and callable(self.connection.close):
                    self.connection.close()
                elif hasattr(self.connection, "disconnect") and callable(self.connection.disconnect):
                    self.connection.disconnect()
                elif (
                    self.ubus_backend is not None
                    and hasattr(self.ubus_backend, "disconnect")
                    and callable(self.ubus_backend.disconnect)
                ):
                    self.ubus_backend.disconnect()
            except (OSError, RuntimeError) as exc:
                logger.debug("Error during UBUS disconnect", error=str(exc))
            finally:
                self.connection = None
