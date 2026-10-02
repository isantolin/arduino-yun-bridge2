"""Type stubs for pytest_embedded.unity."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, AnyStr

class TestCase:
    name: str
    result: str
    attrs: dict[str, Any]
    def __init__(self, name: str, result: str, **kwargs: Any) -> None: ...
    def to_xml(self) -> ET.Element: ...

class TestSuite:
    __test__: bool
    name: str
    attrs: dict[str, Any]
    testcases: list[TestCase]
    def __init__(self, name: str | None = None, **kwargs: Any) -> None: ...
    @property
    def failed_cases(self) -> list[TestCase]: ...
    def add_unity_test_cases(self, s: AnyStr, additional_attrs: dict[str, Any] | None = None) -> None: ...
    def to_xml(self) -> ET.Element: ...
    def dump(self, path: str) -> None: ...
