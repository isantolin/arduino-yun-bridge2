#!/usr/bin/env python3
"""Report coverage gaps from existing coverage artifacts.

This is a developer tool intended to quickly list which files and which
lines/branches are still uncovered, without opening HTML reports.

Inputs:
- Python: coverage/python/coverage.xml (Cobertura XML)
- Arduino: coverage/arduino/coverage.json (gcovr JSON)

The output is intentionally compact so it can be used iteratively while
writing tests.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree
from dataclasses import dataclass
from pathlib import Path

import msgspec


@dataclass(frozen=True)
class FileGaps:
    path: str
    line_total: int
    line_covered: int
    branch_total: int
    branch_covered: int

    @property
    def line_missing(self) -> int:
        return self.line_total - self.line_covered

    @property
    def branch_missing(self) -> int:
        return self.branch_total - self.branch_covered

    @property
    def line_percent(self) -> float:
        return 100.0 if self.line_total == 0 else (100.0 * self.line_covered / self.line_total)

    @property
    def branch_percent(self) -> float:
        return 100.0 if self.branch_total == 0 else (100.0 * self.branch_covered / self.branch_total)


_COND_RE = re.compile(r"\((\d+)/(\d+)\)")


def _parse_condition_coverage(value: str) -> tuple[int, int] | None:
    """Parse Cobertura condition-coverage like: '50% (1/2)'."""
    match = _COND_RE.search(value)
    if not match:
        return None
    hit = int(match.group(1))
    total = int(match.group(2))
    return hit, total


def load_python_gaps(xml_path: Path) -> list[FileGaps]:
    tree = xml.etree.ElementTree.parse(xml_path)
    root = tree.getroot()

    gaps: list[FileGaps] = []

    # Cobertura structure: <coverage><packages><package><classes><class filename=...>
    for class_el in root.findall(".//class"):
        filename = class_el.get("filename")
        if not filename:
            continue

        line_total = 0
        line_covered = 0
        branch_total = 0
        branch_covered = 0

        for line_el in class_el.findall(".//line"):
            number = line_el.get("number")
            hits = line_el.get("hits")
            if number is None or hits is None:
                continue

            line_total += 1
            if int(hits) > 0:
                line_covered += 1

            if line_el.get("branch") == "true":
                cond = line_el.get("condition-coverage")
                if cond:
                    parsed = _parse_condition_coverage(cond)
                    if parsed:
                        hit, total = parsed
                        branch_total += total
                        branch_covered += hit

        gaps.append(
            FileGaps(
                path=filename,
                line_total=line_total,
                line_covered=line_covered,
                branch_total=branch_total,
                branch_covered=branch_covered,
            )
        )

    return gaps


def load_arduino_gaps(json_path: Path) -> list[FileGaps]:
    data = msgspec.json.decode(json_path.read_bytes())

    gaps: list[FileGaps] = []
    for file_entry in data.get("files", []):
        filename = file_entry.get("file") or file_entry.get("filename")
        if not filename:
            continue

        # Detailed gcovr JSON report: per-line records with counts and branch arrays.
        # Note: the report may not list lines that gcov doesn't attribute to code.
        line_records = file_entry.get("lines") or []
        if not isinstance(line_records, list):
            line_records = []

        line_total = 0
        line_covered = 0
        branch_total = 0
        branch_covered = 0

        for line_rec in line_records:
            if not isinstance(line_rec, dict):
                continue
            if "line_number" not in line_rec or "count" not in line_rec:
                continue
            line_total += 1
            if int(line_rec.get("count") or 0) > 0:
                line_covered += 1

            branches = line_rec.get("branches") or []
            if not isinstance(branches, list):
                continue
            for branch in branches:
                if not isinstance(branch, dict):
                    continue
                branch_total += 1
                if int(branch.get("count") or 0) > 0:
                    branch_covered += 1

        gaps.append(
            FileGaps(
                path=str(filename),
                line_total=line_total,
                line_covered=line_covered,
                branch_total=branch_total,
                branch_covered=branch_covered,
            )
        )

    return gaps


def _print_table(title: str, rows: list[FileGaps], top: int) -> None:
    sys.stdout.write(f"\n== {title} (top {top} by missing branches, then lines) ==\n")
    rows_sorted = sorted(rows, key=lambda r: (r.branch_missing, r.line_missing), reverse=True)
    for row in rows_sorted[:top]:
        sys.stdout.write(
            f"{row.branch_missing:4d} br miss ({row.branch_percent:6.2f}%) | "
            f"{row.line_missing:4d} ln miss ({row.line_percent:6.2f}%) | {row.path}\n"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument(
        "--python-xml",
        type=Path,
        default=Path("coverage/python/coverage.xml"),
    )
    parser.add_argument(
        "--arduino-json",
        type=Path,
        default=Path("coverage/arduino/coverage.json"),
    )
    args = parser.parse_args(argv)

    if args.python_xml.exists():
        python_gaps = load_python_gaps(args.python_xml)
        python_only = [g for g in python_gaps if g.branch_missing > 0 or g.line_missing > 0]
        _print_table("Python", python_only, args.top)
    else:
        sys.stderr.write(f"Python coverage XML not found: {args.python_xml}\n")

    if args.arduino_json.exists():
        arduino_gaps = load_arduino_gaps(args.arduino_json)
        arduino_only = [g for g in arduino_gaps if g.branch_missing > 0 or g.line_missing > 0]
        _print_table("Arduino", arduino_only, args.top)
    else:
        sys.stderr.write(f"Arduino coverage JSON not found: {args.arduino_json}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
