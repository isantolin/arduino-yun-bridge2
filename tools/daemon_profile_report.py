#!/usr/bin/env python3
"""Profile the MCU Bridge daemon and generate a resource usage report.

Measures:
  - Module import time and startup RSS/VMS.
  - Dependency count and total source size.
  - Frame parse/build throughput.
  - Protobuf encode/decode throughput.
  - RLE compress/decompress throughput.

Outputs a Markdown table suitable for ``$GITHUB_STEP_SUMMARY``.
"""

from __future__ import annotations

import importlib
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from collections.abc import Callable

import typer

app = typer.Typer(help="Profile the MCU Bridge daemon and report resource usage.")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ImportMetrics:
    import_time_ms: float = 0.0
    rss_after_kb: int = 0
    vms_after_kb: int = 0
    module_count: int = 0
    total_source_bytes: int = 0
    dependency_count: int = 0


@dataclass
class BenchmarkResult:
    name: str
    ops: int
    total_ms: float
    ops_per_sec: float = 0.0
    avg_us: float = 0.0

    def __post_init__(self) -> None:
        if self.total_ms > 0:
            self.ops_per_sec = self.ops / (self.total_ms / 1000)
            self.avg_us = (self.total_ms / self.ops) * 1000


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def _get_rss_vms_kb() -> tuple[int, int]:
    """Return (RSS, VMS) in KiB from /proc/self/status or resource module."""
    try:
        status = Path("/proc/self/status").read_text()
        rss = vms = 0
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
            elif line.startswith("VmSize:"):
                vms = int(line.split()[1])
        return rss, vms
    except OSError:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        return ru.ru_maxrss, 0


def _count_source_bytes(pkg_dir: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for .py files under *pkg_dir*."""
    total = 0
    count = 0
    for p in pkg_dir.rglob("*.py"):
        total += p.stat().st_size
        count += 1
    return count, total


def measure_import(daemon_pkg: str = "mcubridge") -> ImportMetrics:
    """Import the daemon package and measure time + memory."""
    # Evict cached modules so we measure a cold import
    to_remove = [
        k for k in sys.modules if k == daemon_pkg or k.startswith(f"{daemon_pkg}.")
    ]
    for k in to_remove:
        del sys.modules[k]

    t0 = time.perf_counter()
    pkg = importlib.import_module(daemon_pkg)
    elapsed = (time.perf_counter() - t0) * 1000
    rss_after, vms_after = _get_rss_vms_kb()

    # Count third-party dependencies (non-stdlib, non-daemon)
    stdlib_prefixes: set[str] = {
        "_",
        "os",
        "sys",
        "io",
        "re",
        "abc",
        "enum",
        "typing",
        "collections",
        "functools",
        "itertools",
        "pathlib",
        "dataclasses",
        "contextlib",
        "asyncio",
        "logging",
        "json",
        "struct",
        "hashlib",
        "hmac",
        "time",
        "math",
        "copy",
        "warnings",
        "inspect",
        "importlib",
        "types",
        "textwrap",
        "string",
        "base64",
        "xml",
        "unittest",
        "traceback",
        "linecache",
        "token",
        "tokenize",
        "codecs",
        "locale",
        "signal",
        "threading",
        "queue",
        "socket",
        "ssl",
        "http",
        "email",
        "urllib",
        "posixpath",
        "genericpath",
        "stat",
        "nt",
        "ntpath",
        "fnmatch",
        "shutil",
        "tempfile",
        "glob",
        "errno",
        "select",
        "selectors",
        "subprocess",
        "multiprocessing",
        "concurrent",
        "pickle",
        "shelve",
        "dbm",
        "csv",
        "configparser",
        "argparse",
        "gettext",
        "builtins",
        "keyword",
        "operator",
        "numbers",
        "decimal",
        "fractions",
        "random",
        "secrets",
        "bisect",
        "heapq",
        "array",
        "weakref",
        "pprint",
        "reprlib",
        "dis",
        "opcode",
        "marshal",
        "code",
        "codeop",
        "compileall",
        "py_compile",
        "zipimport",
        "pkgutil",
        "modulefinder",
        "runpy",
        "platform",
        "sysconfig",
        "site",
        "atexit",
        "readline",
        "rlcompleter",
    }
    third_party: set[str] = set()
    for k in sys.modules:
        top = k.split(".")[0]
        if top == daemon_pkg or top.startswith("_") or top in stdlib_prefixes:
            continue
        third_party.add(top)

    pkg_dir = Path(pkg.__file__).parent if pkg.__file__ else Path(".")
    mod_count, source_bytes = _count_source_bytes(pkg_dir)

    return ImportMetrics(
        import_time_ms=elapsed,
        rss_after_kb=rss_after,
        vms_after_kb=vms_after,
        module_count=mod_count,
        total_source_bytes=source_bytes,
        dependency_count=len(third_party),
    )


def _benchmark(
    name: str, fn: Callable[[], Any], iterations: int = 5000
) -> BenchmarkResult:
    """Run *fn* for *iterations* and return a BenchmarkResult."""
    # Warmup
    for _ in range(min(100, iterations)):
        fn()

    t0 = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return BenchmarkResult(name=name, ops=iterations, total_ms=elapsed_ms)


def run_benchmarks(iterations: int = 5000) -> list[BenchmarkResult]:
    """Run frame, protobuf, and RLE benchmarks."""
    results: list[BenchmarkResult] = []

    # --- Frame parse/build ---
    from mcubridge.protocol.frame import Frame
    from mcubridge.protocol.protocol import Command

    sample_frame = Frame(
        command_id=Command.CMD_CONSOLE_WRITE,
        sequence_id=42,
        payload=b"Hello, Bridge!" * 4,
    )
    raw = sample_frame.build()

    results.append(_benchmark("Frame.build()", sample_frame.build, iterations))
    results.append(_benchmark("Frame.parse()", lambda: Frame.parse(raw), iterations))

    # --- MsgPack encode/decode (serial payload) ---
    from mcubridge.protocol.structures import ConsoleWritePacket

    sample_packet = ConsoleWritePacket(data=b"Hello, Bridge!" * 4)
    mp_bytes = sample_packet.encode()

    def _mp_encode() -> bytes:
        return ConsoleWritePacket(data=b"Hello, Bridge!" * 4).encode()

    def _mp_decode() -> Any:
        return ConsoleWritePacket.decode(mp_bytes)

    results.append(_benchmark("MsgPack encode", _mp_encode, iterations))
    results.append(_benchmark("MsgPack decode", _mp_decode, iterations))

    # --- RLE compress ---
    from mcubridge.protocol.rle import encode as rle_encode

    rle_input = bytes([0] * 50 + [1] * 30 + [2] * 20 + [255] * 28)

    results.append(_benchmark("RLE encode", lambda: rle_encode(rle_input), iterations))

    # --- Topic parsing ---
    from mcubridge.protocol.topics import parse_topic

    sample_topic = "br/gpio/digital/write"
    results.append(
        _benchmark("Topic parse", lambda: parse_topic("br", sample_topic), iterations)
    )

    return results


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(imp: ImportMetrics, benchmarks: list[BenchmarkResult]) -> str:
    """Generate Markdown summary."""
    lines: list[str] = []

    lines.append("### 🖥️ Daemon Resource Profile")
    lines.append("")

    # Resource table
    lines.append("| Metric | Value |")
    lines.append("| :--- | ---: |")
    lines.append(f"| Import time | {imp.import_time_ms:.0f} ms |")
    lines.append(f"| RSS after import | {imp.rss_after_kb:,} KiB |")
    if imp.vms_after_kb:
        lines.append(f"| VMS after import | {imp.vms_after_kb:,} KiB |")
    lines.append(f"| Source modules | {imp.module_count} .py files |")
    lines.append(f"| Total source size | {imp.total_source_bytes / 1024:.1f} KiB |")
    lines.append(f"| Third-party deps | {imp.dependency_count} packages |")

    lines.append("")

    # Benchmark table
    lines.append("| Benchmark | Ops | Total (ms) | Ops/sec | Avg (µs) |")
    lines.append("| :--- | ---: | ---: | ---: | ---: |")
    for b in benchmarks:
        lines.append(
            f"| {b.name} | {b.ops:,} | {b.total_ms:.1f} | "
            f"{b.ops_per_sec:,.0f} | {b.avg_us:.1f} |"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@app.command()
def main(
    github_step_summary: Annotated[
        Path | None,
        typer.Option(help="Append the report to GitHub step summary output."),
    ] = None,
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write metrics as JSON to this path."),
    ] = None,
    iterations: Annotated[
        int,
        typer.Option(help="Number of iterations per benchmark."),
    ] = 5000,
) -> None:
    """Profile the MCU Bridge daemon and generate a resource report."""
    typer.echo("Measuring daemon import...")
    imp = measure_import()

    typer.echo("Running benchmarks...")
    benchmarks = run_benchmarks(iterations)

    md = render_markdown(imp, benchmarks)
    typer.echo(md)

    if github_step_summary:
        with github_step_summary.open("a", encoding="utf-8") as f:
            f.write("\n" + md + "\n")

    if json_output:
        import msgspec

        data = {
            "import": {
                "import_time_ms": round(imp.import_time_ms, 1),
                "rss_after_kb": imp.rss_after_kb,
                "vms_after_kb": imp.vms_after_kb,
                "module_count": imp.module_count,
                "total_source_bytes": imp.total_source_bytes,
                "dependency_count": imp.dependency_count,
            },
            "benchmarks": [
                {
                    "name": b.name,
                    "ops": b.ops,
                    "total_ms": round(b.total_ms, 1),
                    "ops_per_sec": round(b.ops_per_sec),
                    "avg_us": round(b.avg_us, 1),
                }
                for b in benchmarks
            ],
        }
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_bytes(msgspec.json.encode(data))
        typer.echo(f"JSON written to {json_output}")


if __name__ == "__main__":
    app()
