"""Minimalistic Async Client for MCU Bridge."""

from __future__ import annotations

from grpclib.client import Channel
from . import mcubridge_pb2 as pb
from .definitions import (
    CloudQueuedPublish,
    SpiBitOrder,
    SpiMode,
    build_bridge_args,
)
from .env import dump_client_env
from .mcubridge_grpc import LocalBridgeStub
from .protocol import (
    Command,
    Topic,
)
from .spi import SpiDevice

__all__ = [
    "Channel",
    "LocalBridgeStub",
    "pb",
    "SpiBitOrder",
    "SpiMode",
    "SpiDevice",
    "build_bridge_args",
    "dump_client_env",
    "Command",
    "Topic",
    "CloudQueuedPublish",
]
