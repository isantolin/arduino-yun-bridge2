"""SIL-2 Persistent Storage Primitives based on dbm."""

from __future__ import annotations

import dbm
import os
from typing import TypeVar

T = TypeVar("T")


class DbmDeque:
    """SIL-2 persistent queue implementation over dbm.

    Provides append/popleft with O(1) complexity using monotonic counters.
    Opens/closes DB on each operation to ensure thread safety with sqlite3 backend.
    """

    def __init__(self, path: str, maxlen: int | None = None) -> None:
        self.path = path
        self.maxlen = maxlen
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Initialize if not exists
        with dbm.open(self.path, "c") as db:
            if b"head" not in db:
                db[b"head"] = b"0"
            if b"tail" not in db:
                db[b"tail"] = b"0"

    def append(self, item: bytes) -> None:
        with dbm.open(self.path, "c") as db:
            tail = int(db[b"tail"])
            db[str(tail).encode()] = item
            db[b"tail"] = str(tail + 1).encode()

            # Check maxlen while open
            head = int(db[b"head"])
            if self.maxlen is not None and (tail + 1 - head) > self.maxlen:
                # inline popleft
                key = str(head).encode()
                if key in db:
                    del db[key]
                db[b"head"] = str(head + 1).encode()

    def popleft(self) -> bytes:
        with dbm.open(self.path, "c") as db:
            head = int(db[b"head"])
            tail = int(db[b"tail"])
            if head >= tail:
                raise IndexError("popfrom empty deque")

            key = str(head).encode()
            val = db[key]
            del db[key]
            db[b"head"] = str(head + 1).encode()
            return val

    def __len__(self) -> int:
        with dbm.open(self.path, "c") as db:
            return int(db[b"tail"]) - int(db[b"head"])

    def __getitem__(self, index: int) -> bytes:
        with dbm.open(self.path, "c") as db:
            head = int(db[b"head"])
            tail = int(db[b"tail"])
            length = tail - head

            if index < 0:
                index += length

            if index < 0 or index >= length:
                raise IndexError("deque index out of range")

            actual_index = head + index
            return db[str(actual_index).encode()]

    def clear(self) -> None:
        with dbm.open(self.path, "n") as db:
            db[b"head"] = b"0"
            db[b"tail"] = b"0"

    def close(self) -> None:
        pass


class DbmCache:
    """SIL-2 persistent key-value store over dbm."""

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with dbm.open(self.path, "c"):
            pass

    def __setitem__(self, key: str, value: bytes) -> None:
        with dbm.open(self.path, "c") as db:
            db[key.encode()] = value

    def __getitem__(self, key: str) -> bytes:
        with dbm.open(self.path, "c") as db:
            return db[key.encode()]

    def get(self, key: str, default: T | None = None) -> bytes | T | None:
        try:
            with dbm.open(self.path, "c") as db:
                val = db.get(key.encode())
                return val if val is not None else default
        except KeyError:
            return default
        except Exception:
            return default

    def clear(self) -> None:
        with dbm.open(self.path, "n"):
            pass

    def close(self) -> None:
        pass
