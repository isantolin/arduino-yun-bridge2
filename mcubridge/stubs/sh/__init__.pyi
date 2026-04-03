"""Manual stub for sh library."""
from __future__ import annotations
from typing import Any

class ErrorReturnCode(Exception):
    exit_code: int
    full_cmd: str
    stdout: bytes
    stderr: bytes
    truncate: bool

class Command:
    def __init__(self, path: str) -> None: ...
    def __call__(self, *args: Any, **kwargs: Any) -> RunningCommand: ...

class RunningCommand:
    def __iter__(self) -> RunningCommand: ...
    def __next__(self) -> str: ...
    @property
    def exit_code(self) -> int: ...
    @property
    def stdout(self) -> bytes: ...
    @property
    def stderr(self) -> bytes: ...

def __getattr__(name: str) -> Command: ...

# Common commands used in the project
du: Command
ls: Command
