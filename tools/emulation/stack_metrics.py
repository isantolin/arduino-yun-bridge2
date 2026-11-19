"""Pure helpers for reporting runtime stack watermark measurements."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any


def stack_watermark_metrics(min_sp: int, ramend: int, heap_start: int | None) -> dict[str, int | None]:
    """Calculate used stack, available stack budget, and remaining margin.

    Downward-growing stacks (AVR, ARM Cortex-M0+ SAMD21) start at ``ramend``
    (or ``__StackTop``). ``min_sp`` is the lowest observed stack pointer and
    ``heap_start`` is the end of statically/dynamically allocated data.
    A negative margin means the observed stack crossed into the heap/static
    region and must be treated as an overflow.
    """
    if min_sp < 0 or ramend < 0 or min_sp > ramend:
        raise ValueError("invalid AVR stack pointer or RAM end")
    if heap_start is not None and heap_start < 0:
        raise ValueError("heap_start must be non-negative")

    stack_used = ramend - min_sp
    stack_budget = ramend - heap_start if heap_start is not None else None
    stack_margin = min_sp - heap_start if heap_start is not None else None
    return {
        "stack_used_bytes": stack_used,
        "stack_budget_bytes": stack_budget,
        "stack_margin_bytes": stack_margin,
    }


def parse_mcu_stack_watermark_line(line: str) -> dict[str, int | str] | None:
    """Parse a runtime stack watermark record from firmware or daemon log output.

    Firmware emits ``MCU_STACK_WATERMARK arch=<ARCH> used=<USED> budget=<BUDGET>``
    over ``HDLC_ADDRESS_LOG``.
    """
    marker = "MCU_STACK_WATERMARK "
    if marker not in line and "$TUNVX1NUQUNLX1dBVEVSTUFSSy" in line:
        b64_part = line.split("$", 1)[1].split('"', 1)[0].split("'", 1)[0].split()[0]
        padded = b64_part + ("=" * (-len(b64_part) % 4))
        line = base64.b64decode(padded).decode("utf-8", errors="ignore")
    if marker not in line:
        return None
    tail = line.split(marker, 1)[1]
    arch: str | None = None
    used: int | None = None
    budget: int | None = None
    for raw_token in tail.split():
        token = raw_token.strip("\"',)\n\r")
        if token.startswith("arch="):
            arch = token.removeprefix("arch=")
        elif token.startswith("used="):
            value = token.removeprefix("used=")
            if value.isdigit():
                used = int(value)
        elif token.startswith("budget="):
            value = token.removeprefix("budget=")
            if value.isdigit():
                budget = int(value)
    if not arch or used is None or budget is None or budget <= 0 or used > budget:
        return None
    return {
        "arch": arch,
        "stack_used_bytes": used,
        "stack_budget_bytes": budget,
        "stack_margin_bytes": budget - used,
    }


def extract_latest_mcu_stack_watermark(lines: list[str]) -> dict[str, int | str] | None:
    """Return the most recent runtime stack watermark parsed from log lines."""
    for line in reversed(lines):
        parsed = parse_mcu_stack_watermark_line(line)
        if parsed is not None:
            return parsed
    return None


def write_stack_watermark_report(
    metrics_dir: Path,
    target: str,
    *,
    stack_used_bytes: int,
    stack_budget_bytes: int | None,
    stack_margin_bytes: int | None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write ``<target>_stack_watermark.json`` with canonical schema v1."""
    if stack_used_bytes < 0:
        raise ValueError("stack_used_bytes must be non-negative")
    if stack_budget_bytes is not None and stack_budget_bytes <= 0:
        raise ValueError("stack_budget_bytes must be positive")
    metrics_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "target": target,
        "stack_used_bytes": stack_used_bytes,
        "stack_budget_bytes": stack_budget_bytes,
        "stack_margin_bytes": stack_margin_bytes,
    }
    if extra:
        report.update(extra)
    report_path = metrics_dir / f"{target}_stack_watermark.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report_path
