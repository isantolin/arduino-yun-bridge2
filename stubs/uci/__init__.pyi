from typing import Any, Dict, Optional
from types import TracebackType

class UciException(Exception): ...

class Uci:
    def __init__(self) -> None: ...

    def __enter__(self) -> "Uci": ...

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None: ...

    def get_all(self, package: str, section: str) -> Dict[str, Any]: ...