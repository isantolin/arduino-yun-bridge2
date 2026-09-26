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
from ..state.context import RuntimeState

logger = structlog.get_logger("mcubridge.service.ubus")

try:
    ubus: Any = importlib.import_module("ubus")
except ImportError as exc:
    logger.debug("Native ubus module unavailable; fallback mode active", error=str(exc))
    ubus = None


def _format_ubus_bytes(data: bytes) -> str:
    """Safely decode bytes to UTF-8 or return canonical hex string. [SIL-2]"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"<hex:{data.hex()}>"


def _get_ubus_type(typ: str) -> Any:
    """Resolve UBUS blobmsg type identifier safely."""
    if ubus is None:
        return 0
    return getattr(ubus, f"BLOBMSG_TYPE_{typ}", getattr(ubus, typ, 0))


class BridgeRuntimeFacade(Protocol):
    """Facade protocol decoupling UbusService from full BridgeService implementation."""

    config: RuntimeConfig
    state: RuntimeState
    local_bridge_service: Any

    async def handle_request(self, inbound: Any) -> None: ...

    async def run_process(self, command: str) -> int: ...

    async def kill_process(self, pid: int) -> tuple[bool, str | None]: ...

    async def poll_process(self, pid: int) -> pb.ProcessPollResponse: ...

    async def reset_link(self) -> bool: ...

    async def write_digital_pin(self, pin: int, value: int) -> bool: ...

    async def write_analog_pin(self, pin: int, value: int) -> bool: ...

    clock_sync: Any

    gpio: Any


_UBUS_METHOD_SIGS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("status", ()),
    ("digital_write", (("pin", "INT32"), ("value", "INT32"))),
    ("analog_write", (("pin", "INT32"), ("value", "INT32"))),
    ("mailbox_push", (("message", "STRING"),)),
    ("datastore_set", (("key", "STRING"), ("value", "STRING"))),
    ("datastore_get", (("key", "STRING"),)),
    ("mailbox_read", ()),
    ("file_write", (("path", "STRING"), ("data", "STRING"))),
    ("process_run", (("command", "STRING"),)),
    ("process_kill", (("pid", "INT32"),)),
    ("process_poll", (("pid", "INT32"),)),
    ("link_reset", ()),
    ("ping", ()),
    ("clock_status", ()),
    ("clock_sync", ()),
    (
        "pin_subscribe",
        (
            ("pin", "INT32"),
            ("mode", "STRING"),
            ("interval_ms", "INT32"),
            ("hysteresis", "INT32"),
            ("enabled", "INT32"),
        ),
    ),
)


class UbusService:
    """Manages the lifecycle of McuBridge UBUS object registration on OpenWrt."""

    def __init__(self, runtime: BridgeRuntimeFacade) -> None:
        self.runtime = runtime
        self._conn: Any = None
        self._is_active = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_active(self) -> bool:
        """Return whether UBUS connection is active and registered."""
        return self._is_active

    @property
    def connection(self) -> Any:
        """Return raw UBUS connection handle."""
        return self._conn

    def start(
        self,
        max_attempts: int = 5,
        retry_wait: tenacity.wait.wait_base | None = None,
    ) -> bool:
        """Connect to ubusd and register the 'mcubridge' object with bounded backoff."""
        if ubus is None:
            logger.debug("python-ubus module not available in this environment; skipping UBUS registration")
            return False

        def _connect() -> Any:
            if (conn := ubus.connect()) is None:
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
            self._conn = retryer(_connect)
            self.register_methods()
            self._is_active = True
            logger.info("McuBridge registered successfully on OpenWrt UBUS ('mcubridge')")
            return True
        except (OSError, RuntimeError, tenacity.RetryError) as exc:
            logger.warning("Failed to connect to ubusd after retries", error=str(exc))
            self._conn = None
            self._is_active = False
            return False

    def register_methods(self) -> None:
        """Register RPC methods on the active UBUS connection."""
        if self._conn is None or ubus is None:
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

        methods = {
            name: {
                "method": _make_handler(getattr(self, f"ubus_handle_{name}")),
                "signature": {
                    **{arg: _get_ubus_type(typ) for arg, typ in args},
                    "ubus_rpc_session": _get_ubus_type("STRING"),
                },
            }
            for name, args in _UBUS_METHOD_SIGS
        }

        if hasattr(self._conn, "add") and callable(self._conn.add):
            self._conn.add("mcubridge", methods)
        elif hasattr(ubus, "add") and callable(ubus.add):
            ubus.add("mcubridge", methods)

    async def run(self) -> None:
        """Background loop to process incoming OpenWrt UBUS events directly in asyncio."""
        if self._conn is None or ubus is None or not hasattr(ubus, "loop"):
            return
        logger.info("Starting OpenWrt UBUS event loop")
        try:
            while self._is_active:
                ubus.loop(0)
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
        writer = self.runtime.write_digital_pin if kind == "digital" else self.runtime.write_analog_pin
        self.schedule_async(writer(pin, val))
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
        self.schedule_async(
            self.runtime.local_bridge_service.execute_mailbox_push(pb.MailboxPush(data=message.encode("utf-8")))
        )
        return {"status": "ok", "message_length": len(message)}

    def ubus_handle_datastore_set(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.datastore_set'."""
        key = str(msg.get("key", ""))
        value = str(msg.get("value", ""))
        self.schedule_async(
            self.runtime.local_bridge_service.execute_datastore_put(
                pb.DatastorePut(key=key, value=value.encode("utf-8"))
            )
        )
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
        return {"status": "ok", "key": key, "value": _format_ubus_bytes(val)}

    def ubus_handle_mailbox_read(self, _req: Any, _msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.mailbox_read'."""
        try:
            item: bytes = self.run_sync(self.runtime.state.mailbox_incoming_queue.popleft())
        except IndexError:
            logger.debug("UBUS mailbox read called on empty queue")
            return {"status": "empty"}
        return {"status": "ok", "message": _format_ubus_bytes(item)}

    def ubus_handle_file_write(self, _req: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """UBUS RPC handler for 'mcubridge.file_write'."""
        target_path = str(msg.get("path", ""))
        data_str = str(msg.get("data", ""))
        self.schedule_async(
            self.runtime.local_bridge_service.execute_file_write(
                pb.FileWrite(path=target_path, data=data_str.encode("utf-8"))
            )
        )
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
        data["stdout"] = _format_ubus_bytes(resp.stdout_data)
        data["stderr"] = _format_ubus_bytes(resp.stderr_data)
        data.pop("stdout_data", None)
        data.pop("stderr_data", None)
        return data

    def run_sync(self, coro: Any) -> Any:
        """Execute a coroutine synchronously in a running or fresh event loop. [SIL-2]"""
        target_loop = self._loop
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
        if self._conn is None or not self._is_active:
            return False
        try:
            if hasattr(self._conn, "send") and callable(self._conn.send):
                self._conn.send(f"mcubridge.{event_type}", data)
            elif ubus is not None and hasattr(ubus, "send") and callable(ubus.send):
                ubus.send(f"mcubridge.{event_type}", data)
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
            loop = self._loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(coro, loop)
                return
            asyncio.run(coro)

    def stop(self) -> None:
        """Disconnect from ubusd."""
        self._is_active = False
        if self._conn is not None:
            try:
                if hasattr(self._conn, "close") and callable(self._conn.close):
                    self._conn.close()
                elif hasattr(self._conn, "disconnect") and callable(self._conn.disconnect):
                    self._conn.disconnect()
                elif ubus is not None and hasattr(ubus, "disconnect") and callable(ubus.disconnect):
                    ubus.disconnect()
            except (OSError, RuntimeError) as exc:
                logger.debug("Error during UBUS disconnect", error=str(exc))
            finally:
                self._conn = None
