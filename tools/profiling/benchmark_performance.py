#!/usr/bin/env python3
"""Unified Performance Benchmark and Memory Profiler for MCU Bridge 2 (SIL-2 / MIL-SPEC).

Measures:
- Framing throughput (COBS/R + CRC32)
- AEAD Cryptographic performance (ChaCha20-Poly1305)
- Protobuf wire serialization latency
- Memory usage (RSS, peak allocations, LMDB footprint)
"""

from __future__ import annotations

import os
import psutil
import sys
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "mcubridge") not in sys.path:
    sys.path.insert(0, str(ROOT / "mcubridge"))

from cobs import cobsr
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import typer

from mcubridge.protocol import mcubridge_pb2 as pb, protocol
from mcubridge.protocol.frame import build_frame, parse_frame
from mcubridge.state.storage import LmdbCache

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
        await cache.close()
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


@app.command()
def main(
    output_file: Annotated[Path | None, typer.Option("--output", "-o", help="Write report to markdown file")] = None,
) -> None:
    """Run full benchmark suite and report memory metrics."""
    tracemalloc.start()

    print("🚀 Running MCU Bridge 2 Performance Benchmarks...")
    framing_metrics = benchmark_cobsr_framing()
    rpc_metrics = benchmark_rpc_frames()
    crypto_metrics = benchmark_aead_crypto()
    proto_metrics = benchmark_protobuf_serialization()
    lmdb_metrics = benchmark_lmdb_storage()

    _current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    proc = psutil.Process()
    mem_info = proc.memory_info()
    rss_mb = mem_info.rss / (1024 * 1024)
    vms_mb = mem_info.vms / (1024 * 1024)

    all_metrics = framing_metrics + rpc_metrics + crypto_metrics + proto_metrics + lmdb_metrics

    md_lines: list[str] = [
        "## ⚡ MCU Bridge 2 Performance Benchmark & Memory Report",
        "",
        f"- **Peak Traced Memory:** `{peak_mem / 1024:.2f} KiB`",
        f"- **Process RSS Memory:** `{rss_mb:.2f} MiB`",
        f"- **Process VMS Memory:** `{vms_mb:.2f} MiB`",
        "",
        "### 📊 Throughput & Latency Matrix",
        "",
        "| Operation / Subsystem | Iterations | Total Time (s) | Ops/Sec | Throughput (MB/s) | Latency (µs/op) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: |",
    ]

    for m in all_metrics:
        md_lines.append(
            f"| **{m.name}** | {m.operations:,} | {m.duration_sec:.3f} | "
            f"{m.ops_per_sec:,.0f} | {m.throughput_mb_s:.2f} | {m.latency_us_per_op:.2f} |"
        )

    report_text = "\n".join(md_lines) + "\n"
    print("\n" + report_text)

    if output_file:
        output_file.write_text(report_text, encoding="utf-8")
        print(f"✅ Report saved to {output_file}")


if __name__ == "__main__":
    app()
