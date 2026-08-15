"""Minimalistic Async Client for MCU Bridge."""

from __future__ import annotations

from grpclib.client import Channel as Channel

from . import mcubridge_pb2
from .definitions import (
    SpiBitOrder as SpiBitOrder,
    SpiMode as SpiMode,
    build_bridge_args as build_bridge_args,
)
from .env import dump_client_env as dump_client_env
from .mcubridge_grpc import LocalBridgeStub as LocalBridgeStub
from .mcubridge_pb2 import CloudQueuedPublish as CloudQueuedPublish
from .protocol import (
    Command as Command,
    Topic as Topic,
    parse_pin_spec as parse_pin_spec,
)
from .spi import SpiDevice as SpiDevice

pb = mcubridge_pb2
