"""Type stubs for openwrt_luci_rpc."""

from __future__ import annotations

from typing import Any
from openwrt_luci_rpc.constants import Constants
from openwrt_luci_rpc import exceptions as exceptions

class OpenWrtLuciRPC:
    token: str | None
    def __init__(
        self,
        host_url: str = ...,
        username: str = ...,
        password: str = ...,
        is_https: bool = ...,
        verify_https: bool = ...,
    ) -> None: ...
    def get_all_connected_devices(
        self,
        only_reachable: bool = ...,
        wlan_interfaces: tuple[str, ...] | list[str] = ...,
    ) -> list[dict[str, Any]]: ...

class OpenWrtRpc:
    router: OpenWrtLuciRPC
    def __init__(
        self,
        host_url: str = ...,
        username: str = ...,
        password: str = ...,
        is_https: bool = ...,
        verify_https: bool = ...,
    ) -> None: ...
    def is_logged_in(self) -> bool: ...
    def get_all_connected_devices(
        self,
        only_reachable: bool = ...,
        wlan_interfaces: tuple[str, ...] | list[str] = ...,
    ) -> list[dict[str, Any]]: ...
