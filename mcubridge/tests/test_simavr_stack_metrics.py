from __future__ import annotations

import pytest

from tools.emulation.stack_metrics import stack_watermark_metrics


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
def test_stack_watermark_rejects_invalid_addresses(
    min_sp: int, ramend: int, heap_start: int | None
) -> None:
    with pytest.raises(ValueError, match="invalid AVR stack pointer|heap_start must be non-negative"):
        stack_watermark_metrics(min_sp, ramend, heap_start)
