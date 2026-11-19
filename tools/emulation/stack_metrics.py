"""Pure helpers for reporting runtime stack watermark measurements."""

from __future__ import annotations


def stack_watermark_metrics(
    min_sp: int, ramend: int, heap_start: int | None
) -> dict[str, int | None]:
    """Calculate used stack, available stack budget, and remaining margin.

    AVR stack grows down from RAMEND. ``min_sp`` is the lowest observed stack
    pointer and ``heap_start`` is the end of statically allocated data.
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
