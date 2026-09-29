#!/usr/bin/env python3
"""Unified Performance Benchmark and Memory Profiler for MCU Bridge 2 (SIL-2 / MIL-SPEC).

Measures:
- Framing throughput (COBS/R + CRC32)
- AEAD Cryptographic performance (ChaCha20-Poly1305)
- Protobuf wire serialization latency
- Memory usage (RSS, peak allocations, LMDB footprint)
- Architecture profile, module audit, and top RAM symbols
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from cobs import cobsr
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame, parse_frame
from mcubridge.state.storage import LmdbCache

os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

app = typer.Typer(add_completion=False)


@dataclass(slots=True)
class BenchmarkMetric:
    name: str
    operations: int
    duration_sec: float
    ops_per_sec: float
    throughput_mb_s: float
    latency_us_per_op: float


def benchmark_cobsr_framing(iterations: int = 20_000) -> list[BenchmarkMetric]:
    """Benchmark raw COBS/R encoding and decoding."""
    payload = b"\x01\x02\x03\x00\x05\x06\x07\x00\x09\x10\x11\x12\x13\x14\x15" * 4  # 60 bytes
    payload_len = len(payload)

    # 1. Encode
    start = time.perf_counter()
    encoded_list = [cobsr.encode(payload) for _ in range(iterations)]
    dur_enc = time.perf_counter() - start
    enc_metric = BenchmarkMetric(
        name="COBS/R Encode",
        operations=iterations,
        duration_sec=dur_enc,
        ops_per_sec=iterations / dur_enc,
        throughput_mb_s=(payload_len * iterations) / (dur_enc * 1024 * 1024),
        latency_us_per_op=(dur_enc / iterations) * 1_000_000,
    )

    # 2. Decode
    enc_data = encoded_list[0]
    start = time.perf_counter()
    for _ in range(iterations):
        cobsr.decode(enc_data)
    dur_dec = time.perf_counter() - start
    dec_metric = BenchmarkMetric(
        name="COBS/R Decode",
        operations=iterations,
        duration_sec=dur_dec,
        ops_per_sec=iterations / dur_dec,
        throughput_mb_s=(payload_len * iterations) / (dur_dec * 1024 * 1024),
        latency_us_per_op=(dur_dec / iterations) * 1_000_000,
    )

    return [enc_metric, dec_metric]


def benchmark_aead_crypto(iterations: int = 10_000) -> list[BenchmarkMetric]:
    """Benchmark ChaCha20-Poly1305 AEAD encryption and decryption."""
    key = ChaCha20Poly1305.generate_key()
    chacha = ChaCha20Poly1305(key)
    nonce = os.urandom(12)
    aad = b"HEADER_AAD_DATA"
    data = b"PAYLOAD_TO_ENCRYPT_12345678901234567890" * 2  # 80 bytes
    data_len = len(data)

    # 1. Encrypt
    start = time.perf_counter()
    encrypted = chacha.encrypt(nonce, data, aad)
    for _ in range(iterations - 1):
        chacha.encrypt(nonce, data, aad)
    dur_enc = time.perf_counter() - start
    enc_metric = BenchmarkMetric(
        name="ChaCha20-Poly1305 Encrypt",
        operations=iterations,
        duration_sec=dur_enc,
        ops_per_sec=iterations / dur_enc,
        throughput_mb_s=(data_len * iterations) / (dur_enc * 1024 * 1024),
        latency_us_per_op=(dur_enc / iterations) * 1_000_000,
    )

    # 2. Decrypt
    start = time.perf_counter()
    for _ in range(iterations):
        chacha.decrypt(nonce, encrypted, aad)
    dur_dec = time.perf_counter() - start
    dec_metric = BenchmarkMetric(
        name="ChaCha20-Poly1305 Decrypt",
        operations=iterations,
        duration_sec=dur_dec,
        ops_per_sec=iterations / dur_dec,
        throughput_mb_s=(data_len * iterations) / (dur_dec * 1024 * 1024),
        latency_us_per_op=(dur_dec / iterations) * 1_000_000,
    )

    return [enc_metric, dec_metric]


def benchmark_protobuf_serialization(iterations: int = 20_000) -> list[BenchmarkMetric]:
    """Benchmark Protobuf message serialization and parsing."""
    msg = pb.DigitalWrite(pin=13, value=1)
    serialized = msg.SerializeToString()
    msg_len = len(serialized)

    # 1. Serialize
    start = time.perf_counter()
    for _ in range(iterations):
        msg.SerializeToString()
    dur_ser = time.perf_counter() - start
    ser_metric = BenchmarkMetric(
        name="Protobuf Serialize (DigitalWrite)",
        operations=iterations,
        duration_sec=dur_ser,
        ops_per_sec=iterations / dur_ser,
        throughput_mb_s=(msg_len * iterations) / (dur_ser * 1024 * 1024),
        latency_us_per_op=(dur_ser / iterations) * 1_000_000,
    )

    # 2. Parse
    start = time.perf_counter()
    for _ in range(iterations):
        target = pb.DigitalWrite()
        target.ParseFromString(serialized)
    dur_par = time.perf_counter() - start
    par_metric = BenchmarkMetric(
        name="Protobuf Parse (DigitalWrite)",
        operations=iterations,
        duration_sec=dur_par,
        ops_per_sec=iterations / dur_par,
        throughput_mb_s=(msg_len * iterations) / (dur_par * 1024 * 1024),
        latency_us_per_op=(dur_par / iterations) * 1_000_000,
    )

    return [ser_metric, par_metric]


def benchmark_lmdb_storage(iterations: int = 10_000) -> list[BenchmarkMetric]:
    """Benchmark LMDB embedded cache put and get."""
    import asyncio

    async def _run() -> list[BenchmarkMetric]:
        cache = LmdbCache(":memory:")
        data = b"KEY_VALUE_PERSISTED_STATE_BYTES_12345"

        start = time.perf_counter()
        for i in range(iterations):
            await cache.set(f"key_{i % 100}", data)
        dur_put = time.perf_counter() - start
        put_metric = BenchmarkMetric(
            name="LMDB Cache Put",
            operations=iterations,
            duration_sec=dur_put,
            ops_per_sec=iterations / dur_put,
            throughput_mb_s=(len(data) * iterations) / (dur_put * 1024 * 1024),
            latency_us_per_op=(dur_put / iterations) * 1_000_000,
        )

        start = time.perf_counter()
        for i in range(iterations):
            await cache.get(f"key_{i % 100}")
        dur_get = time.perf_counter() - start
        get_metric = BenchmarkMetric(
            name="LMDB Cache Get",
            operations=iterations,
            duration_sec=dur_get,
            ops_per_sec=iterations / dur_get,
            throughput_mb_s=(len(data) * iterations) / (dur_get * 1024 * 1024),
            latency_us_per_op=(dur_get / iterations) * 1_000_000,
        )
        return [put_metric, get_metric]

    return asyncio.run(_run())


def benchmark_rpc_frames(iterations: int = 20_000) -> list[BenchmarkMetric]:
    """Benchmark full RPC frame construction and parsing with CRC32."""
    msg = pb.DigitalWrite(pin=13, value=1)

    # 1. Build Frame
    start = time.perf_counter()
    raw_frames = [build_frame(protocol.Command.CMD_DIGITAL_WRITE.value, i, payload=msg) for i in range(iterations)]
    dur_build = time.perf_counter() - start
    build_metric = BenchmarkMetric(
        name="build_frame() RPC Frame",
        operations=iterations,
        duration_sec=dur_build,
        ops_per_sec=iterations / dur_build,
        throughput_mb_s=(len(raw_frames[0]) * iterations) / (dur_build * 1024 * 1024),
        latency_us_per_op=(dur_build / iterations) * 1_000_000,
    )

    # 2. Parse Frame
    sample = raw_frames[0]
    start = time.perf_counter()
    for _ in range(iterations):
        parse_frame(sample)
    dur_parse = time.perf_counter() - start
    parse_metric = BenchmarkMetric(
        name="parse_frame() RPC Frame",
        operations=iterations,
        duration_sec=dur_parse,
        ops_per_sec=iterations / dur_parse,
        throughput_mb_s=(len(sample) * iterations) / (dur_parse * 1024 * 1024),
        latency_us_per_op=(dur_parse / iterations) * 1_000_000,
    )

    return [build_metric, parse_metric]


def measure_module_audit() -> list[tuple[str, float, float, str]]:
    """Audit core python modules for import latency and disk footprint."""
    core_modules = [
        "mcubridge.protocol.frame",
        "mcubridge.protocol.structures",
        "mcubridge.protocol.protocol",
        "mcubridge.services.runtime",
        "mcubridge.services.handshake",
        "mcubridge.transport.serial",
        "mcubridge.state.storage",
    ]
    results: list[tuple[str, float, float, str]] = []
    for mod in core_modules:
        start = time.perf_counter()
        try:
            importlib.import_module(mod)
        except (ImportError, AttributeError, ValueError, OSError):
            continue
        dur_ms = (time.perf_counter() - start) * 1000
        size_kb = 0.0
        try:
            spec = importlib.util.find_spec(mod)
            if spec and spec.origin:
                size_kb = Path(spec.origin).stat().st_size / 1024
        except (OSError, ValueError):
            pass
        status = "🟢 Optimized" if dur_ms < 50 else "🟡 Heavy"
        results.append((mod, dur_ms, size_kb, status))
    return results


def measure_top_symbols() -> list[tuple[str, float, int]]:
    """Capture top memory-allocated symbols and traceback locations."""
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics("traceback")
    results: list[tuple[str, float, int]] = []
    for stat in top_stats[:10]:
        frame = stat.traceback[0]
        loc = f"{Path(frame.filename).name}:{frame.lineno}"
        results.append((loc, stat.size / 1024, stat.count))
    return results


@app.command()
def main(
    output_file: Annotated[Path | None, typer.Option("--output", "-o", help="Write report to markdown file")] = None,
    iterations: Annotated[int, typer.Option("--iterations", "-n", help="Benchmark iterations")] = 5000,
    json_path: Annotated[Path | None, typer.Option("--json", help="Path to write JSON benchmark metrics")] = None,
    py_proto: Annotated[Path | None, typer.Option("--py-proto", help="Path to generated protocol.py")] = None,
    py_client: Annotated[Path | None, typer.Option("--py-client", help="Path to client protocol.py")] = None,
    github_step_summary: Annotated[
        Path | None, typer.Option("--github-step-summary", help="Path to GitHub step summary markdown")
    ] = None,
) -> None:
    """Run full benchmark suite and report memory metrics."""
    tracemalloc.start()

    print("🚀 Running MCU Bridge 2 Performance Benchmarks...")
    framing_metrics = benchmark_cobsr_framing(iterations=iterations)
    rpc_metrics = benchmark_rpc_frames(iterations=iterations)
    crypto_metrics = benchmark_aead_crypto(iterations=min(iterations, 10_000))
    proto_metrics = benchmark_protobuf_serialization(iterations=iterations)
    lmdb_metrics = benchmark_lmdb_storage(iterations=min(iterations, 10_000))

    module_stats = measure_module_audit()
    top_symbols = measure_top_symbols()

    _current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    rss_mb = 0.0
    vms_mb = 0.0
    try:
        import psutil

        proc = psutil.Process()
        mem_info = proc.memory_info()
        rss_mb = mem_info.rss / (1024 * 1024)
        vms_mb = mem_info.vms / (1024 * 1024)
    except (ImportError, AttributeError, OSError):
        pass

    all_metrics = framing_metrics + rpc_metrics + crypto_metrics + proto_metrics + lmdb_metrics

    md_lines: list[str] = [
        "### 🖥️ Daemon Resource Profile",
        "",
        "| Metric | Value |",
        "| :--- | ---: |",
        f"| Total Runtime RAM (RSS) | `{rss_mb:.2f} MiB` |",
        f"| Process Virtual Memory (VMS) | `{vms_mb:.2f} MiB` |",
        f"| Peak Traced Allocations | `{peak_mem / 1024:.2f} KiB` |",
        "",
        "### 🐍 Python Architecture Profiling",
        "",
    ]

    if module_stats:
        md_lines.extend(
            [
                "#### 🔍 Module Audit (Time & Size)",
                "| Module | Import Time (ms) | Disk Size (KB) | Status |",
                "| :--- | :---: | :---: | :--- |",
            ]
        )
        total_time = sum(m[1] for m in module_stats)
        total_size = sum(m[2] for m in module_stats)
        for mod, dur, sz, st in module_stats:
            md_lines.append(f"| `{mod}` | {dur:.2f} | {sz:.1f} | {st} |")
        md_lines.append(f"| **TOTAL** | **{total_time:.2f}** | **{total_size:.1f}** | |")
        md_lines.append("")

    if top_symbols:
        md_lines.extend(
            [
                "#### 🧠 RAM Symbols (Top Allocations)",
                "| Source (File:Line) | Allocation (KB) | Obj Count |",
                "| :--- | :---: | :---: |",
            ]
        )
        for sym, sz, cnt in top_symbols:
            md_lines.append(f"| `{sym}` | {sz:.1f} | {cnt} |")
        md_lines.append("")

    md_lines.extend(
        [
            "### ⚡ Performance Benchmark Matrix",
            "",
            "| Operation / Subsystem | Iterations | Total Time (s) | Ops/Sec | Throughput (MB/s) | Latency (µs/op) |",
            "| :--- | :---: | :---: | :---: | :---: | :---: |",
        ]
    )

    for m in all_metrics:
        md_lines.append(
            f"| **{m.name}** | {m.operations:,} | {m.duration_sec:.3f} | "
            f"{m.ops_per_sec:,.0f} | {m.throughput_mb_s:.2f} | {m.latency_us_per_op:.2f} |"
        )

    report_text = "\n".join(md_lines) + "\n"
    print("\n" + report_text)

    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(report_text, encoding="utf-8")
        print(f"✅ Report saved to {output_file}")

    summary_dest = github_step_summary
    if summary_dest is None and "GITHUB_STEP_SUMMARY" in os.environ:
        summary_dest = Path(os.environ["GITHUB_STEP_SUMMARY"])

    if summary_dest:
        summary_dest.parent.mkdir(parents=True, exist_ok=True)
        with summary_dest.open("a", encoding="utf-8") as f:
            f.write(report_text + chr(10))
        print(f"✅ Step summary saved to {summary_dest}")

    if json_path:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_dict: dict[str, object] = {
            "traced_peak_kib": peak_mem / 1024,
            "process_rss_mib": rss_mb,
            "process_vms_mib": vms_mb,
            "metrics": [
                {
                    "name": m.name,
                    "operations": m.operations,
                    "duration_sec": m.duration_sec,
                    "ops_per_sec": m.ops_per_sec,
                    "throughput_mb_s": m.throughput_mb_s,
                    "latency_us_per_op": m.latency_us_per_op,
                }
                for m in all_metrics
            ],
        }
        json_path.write_text(json.dumps(metrics_dict, indent=2), encoding="utf-8")
        print(f"✅ JSON metrics saved to {json_path}")


if __name__ == "__main__":
    app()
