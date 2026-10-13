"""Gateway service for forwarding northbound gRPC requests to local mcubridge daemon.

Refactored to eliminate explicit per-RPC method boilerplate using dynamic dispatch.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Coroutine, Dict, TypeVar

import grpc

logger = logging.getLogger("mcubridge.gateway")

T = TypeVar("T")


class GatewayLocalBridgeService:
    """Northbound gRPC Gateway service.

    Uses dynamic dispatch via `__getattr__` to forward all RPC method calls
    directly to `_forward_rpc`, eliminating repetitive method wrappers.
    """

    def __init__(self, daemon_client: Any, logger_instance: logging.Logger | None = None) -> None:
        self.daemon_client = daemon_client
        self.logger = logger_instance or logger

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

        handler = getattr(self.daemon_client, method_name, None)
        if handler is None or not callable(handler):
            error_msg = f"Daemon client does not support method '{method_name}'"
            self.logger.error(error_msg)
            if context is not None and hasattr(context, "abort"):
                await context.abort(grpc.StatusCode.UNIMPLEMENTED, error_msg)
            raise AttributeError(error_msg)

        try:
            if inspect.iscoroutinefunction(handler):
                return await handler(request, context=context)
            return handler(request, context=context)
        except Exception as exc:
            self.logger.error("Error during RPC execution for %s: %s", method_name, exc)
            if context is not None and hasattr(context, "abort"):
                await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            raise

    def __getattr__(self, name: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Dynamically resolve any gRPC RPC method call to _forward_rpc.

        This replaces explicit, repetitive methods (SetPinMode, DigitalWrite,
        FileRead, etc.) with a single unified dynamic handler.
        """
        if name.startswith("_"):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

        async def dynamic_rpc_handler(request: Any, context: Any = None) -> Any:
            return await self._forward_rpc(name, request, context)

        dynamic_rpc_handler.__name__ = name
        dynamic_rpc_handler.__doc__ = f"Dynamic forwarder for gRPC method {name}."
        return dynamic_rpc_handler


class GatewayDaemon:
    """Main gateway daemon process supervisor."""

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.service: GatewayLocalBridgeService | None = None
        self._running = False

    async def start(self, daemon_client: Any) -> None:
        """Start the gateway service."""
        self.service = GatewayLocalBridgeService(daemon_client)
        self._running = True
        logger.info("GatewayLocalBridgeService started with dynamic RPC dispatch.")

    async def stop(self) -> None:
        """Stop the gateway service."""
        self._running = False
        logger.info("GatewayLocalBridgeService stopped.")
