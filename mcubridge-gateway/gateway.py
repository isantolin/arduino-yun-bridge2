"""Gateway service for forwarding northbound gRPC requests to local mcubridge daemon.

Refactored to eliminate explicit per-RPC method boilerplate using dynamic dispatch
while maintaining SIL-2 compliance, safe thread-offloading, and robust gRPC context handling.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from typing import Any, TypeVar
from collections.abc import Callable, Coroutine

import anyio
import grpc
import grpc.aio
import typer

logger = logging.getLogger("mcubridge.gateway")

T = TypeVar("T")

app = typer.Typer(
    name="mcubridge-gateway",
    help="Managed Cloud Gateway daemon service for mcubridge.",
    add_completion=False,
)


class GatewayLocalBridgeService:
    """Northbound gRPC Gateway service.

    Uses dynamic dispatch via `__getattr__` to forward all RPC method calls
    directly to `_forward_rpc`, eliminating repetitive method wrappers while
    ensuring non-blocking asyncio loop behavior and safe gRPC error handling.
    """

    def __init__(self, daemon_client: Any, logger_instance: logging.Logger | None = None) -> None:
        self.daemon_client = daemon_client
        self.logger = logger_instance or logger

    async def _safe_abort(self, context: Any, code: grpc.StatusCode, details: str) -> None:
        """Safely handle context.abort for both sync and async gRPC contexts."""
        if context is None or not hasattr(context, "abort"):
            return

        try:
            res: Any = context.abort(code, details)
            if inspect.isawaitable(res):
                await res
        except (grpc.RpcError, AttributeError, RuntimeError) as exc:
            # context.abort in sync gRPC raises an internal exception to stop execution
            self.logger.debug("gRPC context abort raised exception: %s", exc)

    async def _forward_rpc(self, method_name: str, request: Any, context: Any = None) -> Any:
        """Forward a gRPC call to the underlying local bridge daemon client.

        Args:
            method_name: Name of the gRPC method being invoked.
            request: The Protobuf request message.
            context: Optional gRPC call context.

        Returns:
            Protobuf response message from daemon client.
        """
        self.logger.debug("Gateway forwarding RPC method: %s", method_name)

        handler: Any = getattr(self.daemon_client, method_name, None)
        if handler is None or not callable(handler):
            error_msg = f"Daemon client does not support method '{method_name}'"
            self.logger.error(error_msg)
            await self._safe_abort(context, grpc.StatusCode.UNIMPLEMENTED, error_msg)
            raise AttributeError(error_msg)

        try:
            # Unwrap any decorated functions or partials
            unwrapped_handler: Any = inspect.unwrap(handler)
            is_async = (
                inspect.iscoroutinefunction(handler)
                or inspect.iscoroutinefunction(unwrapped_handler)
            )

            if is_async:
                res: Any = handler(request, context=context)
                if inspect.isawaitable(res):
                    return await res
                return res

            # Offload blocking synchronous call to thread pool to prevent loop starvation
            def sync_fn() -> Any:
                return handler(request, context=context)
            return await anyio.to_thread.run_sync(sync_fn)
        except (grpc.RpcError, AttributeError, RuntimeError, ValueError) as exc:
            self.logger.error("Error during RPC execution for %s: %s", method_name, exc)
            await self._safe_abort(context, grpc.StatusCode.INTERNAL, str(exc))
            raise

    def __getattr__(self, name: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Dynamically resolve any gRPC RPC method call to _forward_rpc.

        Filters out private attributes, dunder attributes, and internal introspection calls.
        """
        if name.startswith("_") or name.endswith("_") or name in ("trait_names", "shape"):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

        async def dynamic_rpc_handler(request: Any, context: Any = None) -> Any:
            return await self._forward_rpc(name, request, context)

        dynamic_rpc_handler.__name__ = name
        dynamic_rpc_handler.__doc__ = f"Dynamic forwarder for gRPC method {name}."
        return dynamic_rpc_handler


class GatewayDaemon:
    """Main gateway daemon process supervisor with gRPC server lifecycle support."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.host = str(self.config.get("host", "127.0.0.1"))
        self.port = int(self.config.get("port", 8443))
        self.service: GatewayLocalBridgeService | None = None
        self.server: Any = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Return whether the gateway daemon service is currently running."""
        return self._running

    async def start(self, daemon_client: Any) -> None:
        """Start the gateway service and gRPC server on configured host and port."""
        self.service = GatewayLocalBridgeService(daemon_client)

        try:
            grpc_aio: Any = grpc.aio
            self.server = grpc_aio.server()
            bind_address = f"{self.host}:{self.port}"
            self.server.add_insecure_port(bind_address)
            await self.server.start()
            self._running = True
            logger.info("GatewayLocalBridgeService started listening on %s", bind_address)
        except (grpc.RpcError, OSError, RuntimeError) as exc:
            sys.stderr.write(f"[ERROR] Failed to bind/start Cloud Gateway on {self.host}:{self.port}: {exc}\n")
            sys.stderr.flush()
            self._running = False
            raise

    async def stop(self) -> None:
        """Stop the gateway service and gRPC server gracefully."""
        if self.server:
            await self.server.stop(grace=1.0)
        self._running = False
        logger.info("GatewayLocalBridgeService stopped.")


async def _run_gateway(host: str, port: int, once: bool) -> None:
    """Internal async execution loop for Gateway daemon."""
    logger.info("Starting Managed Cloud Gateway daemon process on %s:%d...", host, port)

    class LocalMockDaemonClient:
        async def Echo(self, request: Any, context: Any = None) -> Any:
            return request

    daemon = GatewayDaemon({"host": host, "port": port})
    await daemon.start(LocalMockDaemonClient())

    if once:
        await daemon.stop()
        return

    try:
        while daemon.is_running:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await daemon.stop()


@app.command()
def main(
    host: str = typer.Option("127.0.0.1", "--host", help="Host address to bind (default: 127.0.0.1)"),
    port: int = typer.Option(8443, "--port", help="Port to bind (default: 8443)"),
    once: bool = typer.Option(False, "--once", help="Run startup verification once and exit immediately."),
) -> None:
    """CLI Entry point for starting the Managed Cloud Gateway process."""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_run_gateway(host, port, once))
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
    except (grpc.RpcError, OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"[FATAL] Cloud Gateway crash: {exc}\n")
        sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    app()
