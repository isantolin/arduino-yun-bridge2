#!/usr/bin/env python3
"""MCU Bridge Performance and Memory Benchmarking Tool.

Profiles throughput, latency, and memory footprints for critical protocol framing,
RPC serialization, cryptographic primitives, and persistent storage layers.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

app = typer.Typer(add_completion=False)


@dataclass(slots=True)
class BenchmarkMetric:
    name: str
    operations: int
    duration_sec: float
    throughput_mb_s: float
    ops_per_sec: float
    latency_us_per_op: float


def benchmark_cobsr_framing(iterations: int = 20_000) -> list[BenchmarkMetric]:
    """Benchmark raw COBS/R encoding and decoding."""
    from cobs import cobsr

    payload = b"\x01\x02\x03\x00\x05\x06\x07\x00\x09\x10\x11\x12\x13\x14\x15" * 4  # 60 bytes
    payload_len = len(payload)

    start = time.perf_counter()
    encoded_list = [cobsr.encode(payload) for _ in range(iterations)]
    dur_enc = time.perf_counter() - start

    enc_total_bytes = payload_len * iterations
    enc_metric = BenchmarkMetric(
        name="COBS/R Encode (60B payload)",
        operations=iterations,
        duration_sec=dur_enc,
        throughput_mb_s=(enc_total_bytes / (1024 * 1024)) / dur_enc,
        ops_per_sec=iterations / dur_enc,
        latency_us_per_op=(dur_enc / iterations) * 1_000_000,
    )

    sample = encoded_list[0]
    sample_len = len(sample)
    start = time.perf_counter()
    for _ in range(iterations):
        cobsr.decode(sample)
    dur_dec = time.perf_counter() - start

    dec_total_bytes = sample_len * iterations
    dec_metric = BenchmarkMetric(
        name="COBS/R Decode (Framed payload)",
        operations=iterations,
        duration_sec=dur_dec,
        throughput_mb_s=(dec_total_bytes / (1024 * 1024)) / dur_dec,
        ops_per_sec=iterations / dur_dec,
        latency_us_per_op=(dur_dec / iterations) * 1_000_000,
    )

    return [enc_metric, dec_metric]


def benchmark_rpc_frames(iterations: int = 20_000) -> list[BenchmarkMetric]:
    """Benchmark full RPC frame construction and parsing with CRC32."""
    from mcubridge.protocol import mcubridge_pb2 as pb
    from mcubridge.protocol import protocol
    from mcubridge.protocol.frame import build_frame, parse_frame

    msg = pb.DigitalWrite(pin=13, value=1)

    start = time.perf_counter()
    raw_frames = [build_frame(protocol.Command.CMD_DIGITAL_WRITE.value, i, payload=msg) for i in range(iterations)]
    dur_build = time.perf_counter() - start
    build_bytes = sum(len(f) for f in raw_frames)

    build_metric = BenchmarkMetric(
        name="build_frame() RPC Frame",
        operations=iterations,
        duration_sec=dur_build,
        throughput_mb_s=(build_bytes / (1024 * 1024)) / dur_build,
        ops_per_sec=iterations / dur_build,
        latency_us_per_op=(dur_build / iterations) * 1_000_000,
    )

    sample = raw_frames[0]
    sample_len = len(sample)
    start = time.perf_counter()
    for _ in range(iterations):
        parse_frame(sample)
    dur_parse = time.perf_counter() - start
    parse_bytes = sample_len * iterations

    parse_metric = BenchmarkMetric(
        name="parse_frame() + CRC32 Verify",
        operations=iterations,
        duration_sec=dur_parse,
        throughput_mb_s=(parse_bytes / (1024 * 1024)) / dur_parse,
        ops_per_sec=iterations / dur_parse,
        latency_us_per_op=(dur_parse / iterations) * 1_000_000,
    )

    return [build_metric, parse_metric]


def benchmark_aead_crypto(iterations: int = 10_000) -> list[BenchmarkMetric]:
    """Benchmark ChaCha20-Poly1305 AEAD encryption and decryption."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    key = ChaCha20Poly1305.generate_key()
    chacha = ChaCha20Poly1305(key)
    nonce = b"\x00" * 12
    data = b"Hello MCU Bridge World! Telemetry payload for cryptographic throughput."
    data_len = len(data)

    start = time.perf_counter()
    ct_list = [chacha.encrypt(nonce, data, None) for _ in range(iterations)]
    dur_enc = time.perf_counter() - start

    enc_metric = BenchmarkMetric(
        name="ChaCha20-Poly1305 Encrypt (71B)",
        operations=iterations,
        duration_sec=dur_enc,
        throughput_mb_s=((data_len * iterations) / (1024 * 1024)) / dur_enc,
        ops_per_sec=iterations / dur_enc,
        latency_us_per_op=(dur_enc / iterations) * 1_000_000,
    )

    sample_ct = ct_list[0]
    sample_len = len(sample_ct)
    start = time.perf_counter()
    for _ in range(iterations):
        chacha.decrypt(nonce, sample_ct, None)
    dur_dec = time.perf_counter() - start

    dec_metric = BenchmarkMetric(
        name="ChaCha20-Poly1305 Decrypt (87B)",
        operations=iterations,
        duration_sec=dur_dec,
        throughput_mb_s=((sample_len * iterations) / (1024 * 1024)) / dur_dec,
        ops_per_sec=iterations / dur_dec,
        latency_us_per_op=(dur_dec / iterations) * 1_000_000,
    )

    return [enc_metric, dec_metric]


def benchmark_protobuf_serialization(iterations: int = 20_000) -> list[BenchmarkMetric]:
    """Benchmark Protobuf message serialization and parsing."""
    from mcubridge.protocol import mcubridge_pb2 as pb

    msg = pb.DigitalWrite(pin=13, value=1)
    serialized = msg.SerializeToString()
    ser_len = len(serialized)

    start = time.perf_counter()
    for _ in range(iterations):
        msg.SerializeToString()
    dur_ser = time.perf_counter() - start

    ser_metric = BenchmarkMetric(
        name="Protobuf Serialize (DigitalWrite)",
        operations=iterations,
        duration_sec=dur_ser,
        throughput_mb_s=((ser_len * iterations) / (1024 * 1024)) / dur_ser,
        ops_per_sec=iterations / dur_ser,
        latency_us_per_op=(dur_ser / iterations) * 1_000_000,
    )

    start = time.perf_counter()
    for _ in range(iterations):
        m = pb.DigitalWrite()
        m.ParseFromString(serialized)
    dur_par = time.perf_counter() - start

    par_metric = BenchmarkMetric(
        name="Protobuf Parse (DigitalWrite)",
        operations=iterations,
        duration_sec=dur_par,
        throughput_mb_s=((ser_len * iterations) / (1024 * 1024)) / dur_par,
        ops_per_sec=iterations / dur_par,
        latency_us_per_op=(dur_par / iterations) * 1_000_000,
    )

    return [ser_metric, par_metric]


def benchmark_lmdb_storage(iterations: int = 10_000) -> list[BenchmarkMetric]:
    """Benchmark LMDB embedded cache put and get."""
    import asyncio
    from mcubridge.state.storage import LmdbCache

    async def _run() -> list[BenchmarkMetric]:
        cache = LmdbCache(":memory:")
        await cache.open()

        key = "sensor_analog_0"
        val = b'{"pin": 0, "val": 1023, "time": 1720000000}'
        val_len = len(val)

        start = time.perf_counter()
        for i in range(iterations):
            await cache.put(f"{key}_{i}", val)
        dur_put = time.perf_counter() - start

        put_metric = BenchmarkMetric(
            name="LMDB Cache Put (Single Key)",
            operations=iterations,
            duration_sec=dur_put,
            throughput_mb_s=((val_len * iterations) / (1024 * 1024)) / dur_put,
            ops_per_sec=iterations / dur_put,
            latency_us_per_op=(dur_put / iterations) * 1_000_000,
        )

        start = time.perf_counter()
        for i in range(iterations):
            await cache.get(f"{key}_{i}")
        dur_get = time.perf_counter() - start

        get_metric = BenchmarkMetric(
            name="LMDB Cache Get (Single Key)",
            operations=iterations,
            duration_sec=dur_get,
            throughput_mb_s=((val_len * iterations) / (1024 * 1024)) / dur_get,
            ops_per_sec=iterations / dur_get,
            latency_us_per_op=(dur_get / iterations) * 1_000_000,
        )

        await cache.close()
        return [put_metric, get_metric]

    return asyncio.run(_run())


@app.command()
def main(
    output_file: Annotated[Path | None, typer.Option("--output", "-o", help="Write report to markdown file")] = None,
    iterations: Annotated[int, typer.Option("--iterations", "-n", help="Benchmark iterations")] = 5000,
    json_path: Annotated[Path | None, typer.Option("--json", help="Path to write JSON benchmark metrics")] = None,
    py_proto: Annotated[Path | None, typer.Option("--py-proto", help="Path to generated protocol.py")] = None,
    py_client: Annotated[Path | None, typer.Option("--py-client", help="Path to client protocol.py")] = None,
) -> None:
    """Run full benchmark suite and report memory metrics."""
    # Ensure dependencies are available before running benchmarks
    for pkg in ["cobs", "cryptography", "protobuf", "psutil", "lmdb"]:
        try:
            __import__(pkg)
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", pkg], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    tracemalloc.start()

    print(f"🚀 Running MCU Bridge 2 Performance Benchmarks ({iterations} iterations)...")
    framing_metrics = benchmark_cobsr_framing(iterations=iterations)
    rpc_metrics = benchmark_rpc_frames(iterations=iterations)
    crypto_metrics = benchmark_aead_crypto(iterations=min(iterations, 10_000))
    proto_metrics = benchmark_protobuf_serialization(iterations=iterations)
    lmdb_metrics = benchmark_lmdb_storage(iterations=min(iterations, 10_000))

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
    except Exception:
        pass

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
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(report_text, encoding="utf-8")
        print(f"✅ Report saved to {output_file}")

    if json_path:
        import json
        json_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_dict: dict[str, Any] = {
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
