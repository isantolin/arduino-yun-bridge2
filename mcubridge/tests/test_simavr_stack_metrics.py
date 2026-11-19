from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.emulation.stack_metrics import (
    extract_latest_mcu_stack_watermark,
    parse_mcu_stack_watermark_line,
    stack_watermark_metrics,
    write_stack_watermark_report,
)


def test_stack_watermark_reports_used_budget_and_margin() -> None:
    metrics = stack_watermark_metrics(min_sp=0x1C00, ramend=0x21FF, heap_start=0x1B11)
    assert metrics == {
        "stack_used_bytes": 0x21FF - 0x1C00,
        "stack_budget_bytes": 0x21FF - 0x1B11,
        "stack_margin_bytes": 0x1C00 - 0x1B11,
    }


def test_stack_watermark_preserves_negative_margin_as_overflow_evidence() -> None:
    metrics = stack_watermark_metrics(min_sp=0x1A00, ramend=0x21FF, heap_start=0x1B11)
    assert metrics["stack_margin_bytes"] == -0x111


def test_stack_watermark_without_heap_symbol_omits_budget() -> None:
    metrics = stack_watermark_metrics(min_sp=0x1C00, ramend=0x21FF, heap_start=None)
    assert metrics["stack_used_bytes"] == 0x21FF - 0x1C00
    assert metrics["stack_budget_bytes"] is None
    assert metrics["stack_margin_bytes"] is None


@pytest.mark.parametrize(
    ("min_sp", "ramend", "heap_start"),
    [(-1, 0x21FF, 0x1B11), (0x2200, 0x21FF, 0x1B11), (0x1C00, -1, 0x1B11)],
)
def test_stack_watermark_rejects_invalid_addresses(min_sp: int, ramend: int, heap_start: int | None) -> None:
    with pytest.raises(ValueError, match="invalid AVR stack pointer|heap_start must be non-negative"):
        stack_watermark_metrics(min_sp, ramend, heap_start)


def test_parse_mcu_stack_watermark_line_samd21_and_esp32() -> None:
    samd_line = '2026-11-19 [info] MCU log log="MCU_STACK_WATERMARK arch=SAMD21 used=1824 budget=26112"'
    assert parse_mcu_stack_watermark_line(samd_line) == {
        "arch": "SAMD21",
        "stack_used_bytes": 1824,
        "stack_budget_bytes": 26112,
        "stack_margin_bytes": 26112 - 1824,
    }

    esp32_line = "MCU_STACK_WATERMARK arch=ESP32 used=2416 budget=8192"
    assert parse_mcu_stack_watermark_line(esp32_line) == {
        "arch": "ESP32",
        "stack_used_bytes": 2416,
        "stack_budget_bytes": 8192,
        "stack_margin_bytes": 8192 - 2416,
    }


@pytest.mark.parametrize(
    "invalid_line",
    [
        "unrelated daemon log line",
        "MCU_STACK_WATERMARK arch=SAMD21 used=abc budget=1000",
        "MCU_STACK_WATERMARK arch=SAMD21 used=2000 budget=1000",
        "MCU_STACK_WATERMARK arch=ESP32 used=100 budget=0",
        "MCU_STACK_WATERMARK used=100 budget=1000",
    ],
)
def test_parse_mcu_stack_watermark_line_rejects_invalid_lines(invalid_line: str) -> None:
    assert parse_mcu_stack_watermark_line(invalid_line) is None


def test_extract_latest_mcu_stack_watermark_picks_most_recent_sample() -> None:
    lines = [
        "booting...",
        "MCU_STACK_WATERMARK arch=SAMD21 used=1200 budget=26112",
        "handling rpc...",
        "MCU_STACK_WATERMARK arch=SAMD21 used=1864 budget=26112",
        "teardown",
    ]
    assert extract_latest_mcu_stack_watermark(lines) == {
        "arch": "SAMD21",
        "stack_used_bytes": 1864,
        "stack_budget_bytes": 26112,
        "stack_margin_bytes": 26112 - 1864,
    }
    assert extract_latest_mcu_stack_watermark(["no watermark here"]) is None


def test_write_stack_watermark_report_persists_schema_v1(tmp_path: Path) -> None:
    report_path = write_stack_watermark_report(
        tmp_path,
        "arduino-samd-mkrwifi1010",
        stack_used_bytes=1864,
        stack_budget_bytes=26112,
        stack_margin_bytes=24248,
        extra={"arch": "SAMD21", "measurement_source": "samd21_sram_stack_paint_0xa5"},
    )
    assert report_path == tmp_path / "arduino-samd-mkrwifi1010_stack_watermark.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload == {
        "arch": "SAMD21",
        "measurement_source": "samd21_sram_stack_paint_0xa5",
        "schema_version": 1,
        "stack_budget_bytes": 26112,
        "stack_margin_bytes": 24248,
        "stack_used_bytes": 1864,
        "target": "arduino-samd-mkrwifi1010",
    }

    with pytest.raises(ValueError, match="stack_used_bytes must be non-negative"):
        write_stack_watermark_report(
            tmp_path,
            "esp32-esp32-nano_nora",
            stack_used_bytes=-1,
            stack_budget_bytes=8192,
            stack_margin_bytes=8193,
        )

    with pytest.raises(ValueError, match="stack_budget_bytes must be positive"):
        write_stack_watermark_report(
            tmp_path,
            "esp32-esp32-nano_nora",
            stack_used_bytes=100,
            stack_budget_bytes=0,
            stack_margin_bytes=-100,
        )
