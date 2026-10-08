"""Minimalistic Async Client for MCU Bridge."""

from __future__ import annotations

from grpclib.client import Channel as Channel

from . import mcubridge_pb2

from .definitions import build_bridge_args
from .env import dump_client_env
from .mcubridge_grpc import LocalBridgeStub
from .mcubridge_pb2 import CloudQueuedPublish
from .protocol import (
    CLOUD_DEFAULT_TOPIC_PREFIX,
    Command,
    DEFAULT_CLOUD_HOST,
    DEFAULT_CLOUD_PORT,
    SpiBitOrder,
    SpiDataMode,
    Topic,
)
from .spi import SpiDevice

pb = mcubridge_pb2

__all__ = [
    "Channel",
    "CLOUD_DEFAULT_TOPIC_PREFIX",
    "CloudQueuedPublish",
    "Command",
    "DEFAULT_CLOUD_HOST",
    "DEFAULT_CLOUD_PORT",
    "LocalBridgeStub",
    "SpiBitOrder",
    "SpiDataMode",
    "SpiDevice",
    "Topic",
    "build_bridge_args",
    "dump_client_env",
    "pb",
]
