#!/usr/bin/env python3
"""Aggregate Python and Arduino coverage results into a single summary."""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CoverageMetrics:
    suite: str
    lines_total: int
    lines_covered: int
    line_percent: Optional[float]
    branches_total: int
    branches_covered: int
    branch_percent: Optional[float]
    artifact_hint: str

    @property
    def lines_display(self) -> str:
        if self.lines_total == 0:
            return "n/a"
        return f"{self.lines_covered}/{self.lines_total}"

    @property
    def branches_display(self) -> str:
        if self.branches_total == 0:
            return "n/a"
        return f"{self.branches_covered}/{self.branches_total}"

    @staticmethod
    def format_percent(value: Optional[float]) -> str:
        if value is None:
            return "n/a"
        return f"{value:.2f}%"


def _read_python_metrics(path: Path) -> Optional[CoverageMetrics]:
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    attr = root.attrib

    def _get(name: str) -> int:
        raw = attr.get(name)
        if raw is None:
            return 0
        try:
            return int(float(raw))
        except ValueError:
            return 0

    lines_total = _get("lines-valid")
    lines_covered = _get("lines-covered")
    line_rate = attr.get("line-rate")
    line_percent = (
        float(line_rate) * 100
        if line_rate is not None
        else None
    )

    branches_total = _get("branches-valid")
    branches_covered = _get("branches-covered")
    branch_rate = attr.get("branch-rate")
    branch_percent = (
        float(branch_rate) * 100
        if branch_rate is not None
        else None
    )

    return CoverageMetrics(
        suite="Python",
        lines_total=lines_total,
        lines_covered=lines_covered,
        line_percent=line_percent,
        branches_total=branches_total,
        branches_covered=branches_covered,
        branch_percent=branch_percent,
        artifact_hint=str(path),
    )


def _safe_percent(hit: int, total: int) -> Optional[float]:
    if total <= 0:
        return None
    return (hit / total) * 100.0


def _read_arduino_metrics(path: Path) -> Optional[CoverageMetrics]:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    summaries = data.get("summaries")
    summary = None
    if isinstance(summaries, dict):
        summary = summaries.get("all")
        if summary is None and summaries:
            summary = next(iter(summaries.values()))

    line_counts: dict
    branch_counts: dict
    percent: dict

    if summary is not None:
        count = summary.get("count", {})
        percent = summary.get("percent", {})
        line_counts = count.get("lines", {})
        branch_counts = count.get("branches", {})
        lines_total = int(
            line_counts.get("found")
            or line_counts.get("total")
            or 0
        )
        lines_covered = int(
            line_counts.get("hit")
            or line_counts.get("covered")
            or 0
        )
        line_percent = percent.get("lines")
        if line_percent is None:
            line_percent = _safe_percent(lines_covered, lines_total)

        branches_total = int(
            branch_counts.get("found")
            or branch_counts.get("total")
            or 0
        )
        branches_covered = int(
            branch_counts.get("hit")
            or branch_counts.get("covered")
            or 0
        )
        branch_percent = percent.get("branches")
        if branch_percent is None:
            branch_percent = _safe_percent(branches_covered, branches_total)
    else:
        lines_total = int(data.get("line_total") or 0)
        lines_covered = int(data.get("line_covered") or 0)
        line_percent = data.get("line_percent")
        if line_percent is None:
            line_percent = _safe_percent(lines_covered, lines_total)

        branches_total = int(data.get("branch_total") or 0)
        branches_covered = int(data.get("branch_covered") or 0)
        branch_percent = data.get("branch_percent")
        if branch_percent is None:
            branch_percent = _safe_percent(branches_covered, branches_total)

    return CoverageMetrics(
        suite="Arduino",
        lines_total=lines_total,
        lines_covered=lines_covered,
        line_percent=line_percent,
        branches_total=branches_total,
        branches_covered=branches_covered,
        branch_percent=branch_percent,
        artifact_hint=str(path),
    )


def _build_combined_metrics(
    metrics: list[CoverageMetrics],
) -> Optional[CoverageMetrics]:
    include = [m for m in metrics if m is not None]
    if not include:
        return None
    lines_total = sum(m.lines_total for m in include)
    lines_covered = sum(m.lines_covered for m in include)
    branches_total = sum(m.branches_total for m in include)
    branches_covered = sum(m.branches_covered for m in include)
    line_percent = _safe_percent(lines_covered, lines_total)
    branch_percent = _safe_percent(branches_covered, branches_total)
    return CoverageMetrics(
        suite="Combined",
        lines_total=lines_total,
        lines_covered=lines_covered,
        line_percent=line_percent,
        branches_total=branches_total,
        branches_covered=branches_covered,
        branch_percent=branch_percent,
        artifact_hint="coverage/python + coverage/arduino",
    )


def _render_markdown(rows: list[CoverageMetrics]) -> str:
    header = (
        "| Suite | Lines (hit/total) | Line % | "
        "Branches (hit/total) | Branch % |"
    )
    separator = "| --- | --- | --- | --- | --- |"
    body = []
    for row in rows:
        line = (
            "| {suite} | {lines} | {line_pct} | {branches} | {branch_pct} |"
        ).format(
            suite=row.suite,
            lines=row.lines_display,
            line_pct=CoverageMetrics.format_percent(row.line_percent),
            branches=row.branches_display,
            branch_pct=CoverageMetrics.format_percent(row.branch_percent),
        )
        body.append(line)
    artifact_list = "\n".join(
        f"- `{row.suite}` artifacts: {row.artifact_hint}"
        for row in rows
    )
    return "\n".join([header, separator, *body, "", artifact_list])


def _write_optional(path: Optional[str], content: str) -> None:
    if not path:
        return
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as handle:
        handle.write(content)


def _append_optional(path: Optional[str], content: str) -> None:
    if not path:
        return
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as handle:
        handle.write(content)
        if not content.endswith("\n"):
            handle.write("\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python-xml",
        default="coverage/python/coverage.xml",
    )
    parser.add_argument(
        "--arduino-summary",
        default="coverage/arduino/summary.json",
    )
    parser.add_argument(
        "--output-markdown",
        help="Write the table to the given markdown file.",
    )
    parser.add_argument(
        "--output-json",
        help="Write machine-readable metrics to this path.",
    )
    parser.add_argument(
        "--github-step-summary",
        help="Append the table to GitHub step summary output.",
    )
    args = parser.parse_args(argv)

    python_metrics = _read_python_metrics(Path(args.python_xml))
    arduino_metrics = _read_arduino_metrics(Path(args.arduino_summary))

    rows = [
        row
        for row in [python_metrics, arduino_metrics]
        if row is not None
    ]
    combined = _build_combined_metrics(rows)
    if combined is not None:
        rows.append(combined)

    if not rows:
        print(
            "[coverage-report] No coverage artifacts were found.",
            file=sys.stderr,
        )
        return 1

    table = _render_markdown(rows)
    print(table)

    _write_optional(args.output_markdown, table + "\n")
    _append_optional(args.github_step_summary, table + "\n")

    if args.output_json:
        payload = {
            row.suite.lower(): {
                "lines_total": row.lines_total,
                "lines_covered": row.lines_covered,
                "line_percent": row.line_percent,
                "branches_total": row.branches_total,
                "branches_covered": row.branches_covered,
                "branch_percent": row.branch_percent,
            }
            for row in rows
        }
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
