from __future__ import annotations


class DecodeError(Exception):
    ...


def encode(data: bytes) -> bytes: ...


def decode(data: bytes) -> bytes: ...


__all__ = ["DecodeError", "encode", "decode"]
