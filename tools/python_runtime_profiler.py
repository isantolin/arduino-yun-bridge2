#!/usr/bin/env python3
"""Profiling tool for the MCU Bridge Python architecture (SIL-2)."""

from __future__ import annotations

import argparse
import time
import os
from pathlib import Path
import tracemalloc

# Start tracing early to capture all allocations
tracemalloc.start()


def measure_imports() -> list[tuple[str, float]]:
    modules = [
        "asyncio",
        "msgspec",
        "aiomqtt",
        "paho.mqtt",
        "psutil",
        "mcubridge.protocol.frame",
        "mcubridge.protocol.structures",
    ]
    results: list[tuple[str, float]] = []
    for mod in modules:
        start = time.perf_counter()
        try:
            __import__(mod)
        except ImportError:
            pass
        end = time.perf_counter()
        results.append((mod, (end - start) * 1000))
    return results


def measure_runtime_memory() -> int:
    return 0


def measure_object_symbols() -> list[tuple[str, int, int]]:
    """Capture a snapshot of the most memory-intensive symbols/objects."""
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics("traceback")

    results: list[tuple[str, int, int]] = []
    for stat in top_stats[:10]:
        frame = stat.traceback[0]
        results.append(
            (f"{Path(frame.filename).name}:{frame.lineno}", stat.size, stat.count)
        )
    return results


def get_module_size(mod_name: str) -> int:
    """Get the size of the module's source file on disk."""
    try:
        import importlib.util

        spec = importlib.util.find_spec(mod_name)
        if spec and spec.origin:
            return os.path.getsize(spec.origin)
    except OSError:
        pass
    return 0


def generate_report(github_step_summary: Path | None = None) -> None:
    import msgspec
    from mcubridge.protocol import structures

    import_stats = measure_imports()
    mem_rss = measure_runtime_memory()
    object_symbols = measure_object_symbols()

    # Measure MsgPack efficiency
    test_packet = structures.AckPacket(command_id=0x42)
    start_enc = time.perf_counter()
    for _ in range(1000):
        _ = msgspec.msgpack.encode(test_packet)
    avg_enc = time.perf_counter() - start_enc

    md = [
        "### 🐍 Python Architecture Profiling",
        "",
        f"**Total Runtime RAM (RSS):** `{mem_rss / 1024 / 1024:.2f} MB`",
        f"**Serialization Latency:** `{avg_enc * 1000:.3f} ms / 1k pkts`",
        "",
        "#### 🔍 Module Audit (Time & Size)",
        "| Module | Import Time (ms) | Disk Size (KB) | Status |",
        "| :--- | :---: | :---: | :--- |",
    ]

    total_time = 0.0
    total_size = 0
    for mod, duration in import_stats:
        d_size = get_module_size(mod)
        total_time += duration
        total_size += d_size
        status = "🟢 Optimized" if duration < 50 else "🟡 Heavy"
        md.append(f"| `{mod}` | {duration:.2f} | {d_size / 1024:.1f} | {status} |")

    md.append(f"| **TOTAL** | **{total_time:.2f}** | **{total_size / 1024:.1f}** | |")

    md.extend(
        [
            "",
            "#### 🧠 RAM Symbols (Top Allocations)",
            "| Source (File:Line) | Allocation (KB) | Obj Count |",
            "| :--- | :---: | :---: |",
        ]
    )

    for symbol, size, count in object_symbols:
        md.append(f"| `{symbol}` | {size / 1024:.1f} | {count} |")

    md.extend(["", "---"])

    report = "\n".join(md)
    print(report)

    if github_step_summary:
        with github_step_summary.open("a", encoding="utf-8") as f:
            f.write("\n" + report + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-step-summary", type=Path, default=None)
    args = parser.parse_args()
    generate_report(args.github_step_summary)
