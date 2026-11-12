"""Lightweight, strongly-typed Unity C test runner output parser. [SIL-2]

Parses Unity test console output and transforms results into structured
TestCase/TestSuite representations and JUnit XML. Replaces external pytest-embedded.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any


class TestCase:
    """Represents an individual Unity test case result."""

    def __init__(
        self,
        name: str,
        result: str,
        message: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.result = result
        self.message = message
        self.attrs = attrs or {}

    def to_xml(self) -> ET.Element:
        """Convert the test case into a JUnit-compatible XML Element."""
        elem = ET.Element("testcase", attrib={"name": self.name, "result": self.result, **self.attrs})
        if self.result == "FAIL":
            failure = ET.SubElement(elem, "failure")
            if self.message:
                failure.text = self.message
        return elem


class TestSuite:
    """Represents a suite of Unity test cases with JUnit XML export."""

    __test__ = False  # Instruct pytest not to discover this as a test class

    def __init__(self, name: str | None = None, **kwargs: Any) -> None:
        self.name = name or "unity_testsuite"
        self.attrs: dict[str, Any] = kwargs
        self.testcases: list[TestCase] = []

    @property
    def failed_cases(self) -> list[TestCase]:
        """Return list of failed test cases."""
        return [tc for tc in self.testcases if tc.result == "FAIL"]

    def add_unity_test_cases(
        self,
        s: str,
        additional_attrs: dict[str, Any] | None = None,
    ) -> None:
        """Parse raw Unity console output string and register discovered test cases."""
        extra = additional_attrs or {}
        for line in s.splitlines():
            clean_line = line.strip()
            if not clean_line or clean_line.startswith("-") or clean_line in ("OK", "FAIL"):
                continue
            parts = clean_line.split(":")
            if len(parts) >= 4:
                # Format: file:line:test_name:RESULT[:optional_message]
                test_name = parts[2].strip()
                result = parts[3].strip()
                message = ":".join(parts[4:]).strip() if len(parts) > 4 else None
                self.testcases.append(
                    TestCase(
                        name=test_name,
                        result=result,
                        message=message,
                        attrs={"file": parts[0].strip(), "line": parts[1].strip(), **extra},
                    )
                )

    def to_xml(self) -> ET.Element:
        """Convert the test suite into a JUnit-compatible XML Element."""
        root = ET.Element(
            "testsuite",
            attrib={
                "name": self.name,
                "tests": str(len(self.testcases)),
                "failures": str(len(self.failed_cases)),
            },
        )
        for tc in self.testcases:
            root.append(tc.to_xml())
        return root

    def dump(self, path: str) -> None:
        """Export the test suite XML to a target file path."""
        tree = ET.ElementTree(self.to_xml())
        tree.write(path, encoding="utf-8", xml_declaration=True)
