"""Type stub for cobs package [SIL-2]."""

from . import cobs as cobs, cobsr as cobsr
from .cobs import DecodeError as DecodeError, decode as decode, encode as encode

__all__ = ["cobs", "cobsr", "encode", "decode", "DecodeError"]
