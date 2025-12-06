class DecodeError(Exception):
    ...


def encode(data: bytes) -> bytes:
    ...


def decode(data: bytes) -> bytes:
    ...


def set_debug_enable(enabled: bool) -> None:
    ...
