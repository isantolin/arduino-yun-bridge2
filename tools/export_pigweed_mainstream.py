#!/usr/bin/env python3
"""Packages upstream Google Pigweed into a standalone Arduino IDE / Arduino CLI library for 32-bit MCUs (SAMD & ESP32).

[SIL-2 / Multi-Architecture Compilation]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer

REPO_ROOT = Path(__file__).resolve().parent.parent
PW_ROOT = REPO_ROOT / "third_party" / "pigweed"


def generate_pw_rpc_packet_header(output_dir: Path) -> Path:
    """Generate Pigweed's PWPB RPC packet header into an export directory."""
    output_dir = output_dir.resolve()
    proto_path = PW_ROOT / "pw_rpc" / "internal" / "packet.proto"
    plugin_path = PW_ROOT / "pw_protobuf" / "py" / "pw_protobuf" / "plugin.py"
    protoc = shutil.which("protoc")
    if not proto_path.is_file() or not plugin_path.is_file():
        raise FileNotFoundError(
            "Pigweed pw_rpc packet.proto or PWPB plugin is missing; "
            "initialize the Pigweed submodule: git submodule update --init third_party/pigweed"
        )
    if protoc is None:
        raise FileNotFoundError("protoc is required to export upstream pw_rpc; install protobuf-compiler")

    protobuf_proto_dir = PW_ROOT / "pw_protobuf" / "pw_protobuf_protos"
    codegen_proto_dir = PW_ROOT / "pw_protobuf" / "pw_protobuf_codegen_protos"

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pigweed-pwpb-codegen-") as tmp:
        generated_python_root = Path(tmp) / "py"
        protobuf_python_dir = generated_python_root / "pw_protobuf_protos"
        codegen_python_dir = generated_python_root / "pw_protobuf_codegen_protos"
        protobuf_python_dir.mkdir(parents=True)
        codegen_python_dir.mkdir(parents=True)

        for package_dir in (protobuf_python_dir, codegen_python_dir):
            (package_dir / "__init__.py").write_text("", encoding="utf-8")

        generation_commands = (
            (
                f"-I{protobuf_proto_dir}",
                f"--python_out={protobuf_python_dir}",
                "common.proto",
                "field_options.proto",
                "status.proto",
            ),
            (
                f"-I{codegen_proto_dir}",
                f"--python_out={codegen_python_dir}",
                "codegen_options.proto",
            ),
        )
        for arguments in generation_commands:
            try:
                subprocess.run(
                    [protoc, "--experimental_allow_proto3_optional", *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                    cwd=protobuf_proto_dir if "common.proto" in arguments else codegen_proto_dir,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"Pigweed Python code-generation proto compilation failed:\n{exc.stderr or exc.stdout}"
                ) from exc

        python_paths = (
            generated_python_root,
            PW_ROOT / "pw_status" / "py",
            PW_ROOT / "pw_protobuf" / "py",
            PW_ROOT / "pw_protobuf_compiler" / "py",
            PW_ROOT / "pw_rpc" / "py",
            PW_ROOT / "pw_codegen" / "py",
            PW_ROOT / "pw_cli" / "py",
        )
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        python_path_entries = [str(path) for path in python_paths]
        if existing_pythonpath:
            python_path_entries.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(python_path_entries)

        command = [
            protoc,
            f"-I{PW_ROOT}",
            f"--plugin=protoc-gen-pwpb={plugin_path}",
            f"--pwpb_out={output_dir}",
            "pw_rpc/internal/packet.proto",
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=PW_ROOT,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Pigweed pw_rpc PWPB packet generation failed:\n{exc.stderr or exc.stdout}") from exc

    packet_header = output_dir / "pw_rpc" / "internal" / "packet.pwpb.h"
    if not packet_header.is_file():
        raise FileNotFoundError(f"PWPB generator did not create the expected header: {packet_header}")
    return packet_header


def export_mainstream_library(dest_dir: Path) -> None:
    """Export clean upstream Pigweed modules for 32-bit Arduino architectures."""
    dest_dir = dest_dir.resolve()
    print(f"[*] Exporting Mainstream Pigweed Arduino Library to: {dest_dir}")
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    src_dir = dest_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write library.properties
    lib_props = """name=Pigweed
version=0.0.1
author=Google Pigweed Authors
maintainer=Ignacio Santolin <ignacio.santolin@gmail.com>
sentence=Google Pigweed mainstream modules for 32-bit Arduino architectures.
paragraph=Mainstream Pigweed RPC, HDLC, Tokenizer, Chrono and serialization for 32-bit MCUs (SAMD21, ESP32-S3).
category=Communication
url=https://pigweed.dev
architectures=samd,esp32
includes=pw_hdlc/encoder.h,pw_rpc/server.h,pw_chrono/system_clock.h
"""
    (dest_dir / "library.properties").write_text(lib_props, encoding="utf-8")

    # 2. Copy Pigweed modules public headers and implementation sources
    modules = [
        "pw_status",
        "pw_result",
        "pw_span",
        "pw_bytes",
        "pw_varint",
        "pw_containers",
        "pw_stream",
        "pw_checksum",
        "pw_hdlc",
        "pw_function",
        "pw_string",
        "pw_base64",
        "pw_tokenizer",
        "pw_chrono",
        "pw_sync",
        "pw_preprocessor",
        "pw_numeric",
        "pw_enum",
        "pw_memory",
        "pw_polyfill",
        "pw_protobuf",
        "pw_assert",
        "pw_rpc",
        "pw_log",
        "pw_toolchain",
    ]

    EXCLUDED_SOURCES = {
        "pw_assert_examples.cc",
        "pw_assert_fake_backend.cc",
        "pw_polyfill_test.cc",
        "pw_containers_test_helpers.cc",
        "pw_tokenizer_generate_decoding_test_data.cc",
        "pw_tokenizer_tokenize_c99_test_entry_point.cc",
        "pw_tokenizer_base64.cc",
        "pw_tokenizer_csv.cc",
        "pw_tokenizer_detokenize.cc",
        "pw_tokenizer_detokenize_from_this_program.cc",
        "pw_tokenizer_token_database.cc",
        "pw_tokenizer_decode.cc",
        "pw_containers_aa_tree.cc",
        "pw_containers_aa_tree_item.cc",
        "pw_containers_intrusive_map.cc",
        "pw_stream_socket_stream.cc",
        "pw_stream_std_file_stream.cc",
        "pw_hdlc_hdlc_sys_io_system_server.cc",
        "pw_hdlc_router.cc",
        "pw_hdlc_wire_packet_parser.cc",
        "pw_chrono_wrap_time_build_time.cc",
        "pw_chrono_wrap_time_system_clock.cc",
        "pw_chrono_system_clock.cc",
        "pw_log_proto_utils.cc",
        "pw_string_string_builder.cc",
        "pw_string_type_to_string.cc",
        "pw_string_format.cc",
        "pw_string_utf_codecs.cc",
        "pw_bytes_byte_builder.cc",
        "pw_bytes_alignment.cc",
        "pw_containers_inline_var_len_entry_queue.cc",
        "pw_containers_intrusive_queue.cc",
        "pw_containers_var_len_entry.cc",
        "pw_containers_var_len_entry_queue_iterator.cc",
    }

    for mod in modules:
        mod_path = PW_ROOT / mod
        pub_dirs = [mod_path / "public", mod_path / "cpp" / "public"]
        for p in pub_dirs:
            if p.exists():
                shutil.copytree(p, src_dir, dirs_exist_ok=True)

        if mod == "pw_sync":
            continue

        for cc_file in mod_path.glob("*.cc"):
            if mod == "pw_rpc" and cc_file.name not in {
                "call.cc",
                "client.cc",
                "client_call.cc",
                "channel.cc",
                "channel_list.cc",
                "endpoint.cc",
                "packet.cc",
                "packet_meta.cc",
                "server.cc",
                "server_call.cc",
                "service.cc",
            }:
                continue
            target_name = f"{mod}_{cc_file.name}"
            if (
                not cc_file.name.endswith(("_test.cc", "_fuzzer.cc", "_perf_test.cc"))
                and cc_file.name not in EXCLUDED_SOURCES
                and target_name not in EXCLUDED_SOURCES
            ):
                shutil.copy2(cc_file, src_dir / target_name)

        for cc_file in (mod_path / "cpp").glob("*.cc"):
            target_name = f"{mod}_{cc_file.name}"
            if (
                not cc_file.name.endswith(("_test.cc", "_fuzzer.cc"))
                and cc_file.name not in EXCLUDED_SOURCES
                and target_name not in EXCLUDED_SOURCES
            ):
                shutil.copy2(cc_file, src_dir / target_name)

    # 3. Nanopb and Raw RPC submodules
    nanopb_rpc_path = PW_ROOT / "pw_rpc" / "nanopb"
    shutil.copytree(nanopb_rpc_path / "public", src_dir, dirs_exist_ok=True)
    for cc_file in nanopb_rpc_path.glob("*.cc"):
        if cc_file.name in {"common.cc", "method.cc", "server_reader_writer.cc"}:
            shutil.copy2(cc_file, src_dir / f"pw_rpc_nanopb_{cc_file.name}")
    raw_rpc_path = PW_ROOT / "pw_rpc" / "raw"
    shutil.copytree(raw_rpc_path / "public", src_dir, dirs_exist_ok=True)
    shutil.copy2(raw_rpc_path / "method.cc", src_dir / "pw_rpc_raw_method.cc")

    # 4. Generate pw_rpc packet header
    generate_pw_rpc_packet_header(src_dir)

    # 5. Fuchsia stdcompat and fit dependencies (required by pw_function, pw_result, pw_varint, etc.)
    for fuchsia_lib in ["stdcompat", "fit"]:
        fuchsia_include = PW_ROOT / "third_party" / "fuchsia" / "repo" / "sdk" / "lib" / fuchsia_lib / "include"
        if fuchsia_include.exists():
            shutil.copytree(fuchsia_include, src_dir, dirs_exist_ok=True)

    # 6. pw_sync baremetal backend (mutex_native.h, mutex_inline.h, etc.)
    for d in ["public", "public_overrides"]:
        bm_dir = PW_ROOT / "pw_sync_baremetal" / d
        if bm_dir.is_dir():
            shutil.copytree(bm_dir, src_dir, dirs_exist_ok=True)

    # Configure pw_rpc single-thread embedded config
    rpc_config = src_dir / "pw_rpc" / "internal" / "config.h"
    if rpc_config.is_file():
        content = rpc_config.read_text(encoding="utf-8")
        content = content.replace(
            "#define PW_RPC_USE_GLOBAL_MUTEX 1",
            "#define PW_RPC_USE_GLOBAL_MUTEX 0",
            1,
        )
        rpc_config.write_text(content, encoding="utf-8")

    # 7. Native 32-bit Backends for Arduino (Chrono, Assert, Log)
    # 7.1 Chrono backend: 1 ms tick using millis()
    chrono_backend_dir = src_dir / "pw_chrono_backend"
    chrono_backend_dir.mkdir(parents=True, exist_ok=True)
    chrono_config = """#pragma once
#define PW_CHRONO_SYSTEM_CLOCK_PERIOD_SECONDS_NUMERATOR 1
#define PW_CHRONO_SYSTEM_CLOCK_PERIOD_SECONDS_DENOMINATOR 1000

#ifdef __cplusplus
#include "pw_chrono/epoch.h"
namespace pw::chrono::backend {
inline constexpr Epoch kSystemClockEpoch = Epoch::kTimeSinceBoot;
inline constexpr bool kSystemClockFreeRunning = false;
inline constexpr bool kSystemClockNmiSafe = false;
}  // namespace pw::chrono::backend
#endif
"""
    (chrono_backend_dir / "system_clock_config.h").write_text(chrono_config, encoding="utf-8")

    system_clock_cpp = """#include "pw_chrono/system_clock.h"

#if defined(ARDUINO)
#include <Arduino.h>
namespace pw::chrono::backend {
int64_t GetSystemClockTickCount() {
  return static_cast<int64_t>(millis());
}
}  // namespace pw::chrono::backend
#else
#include <chrono>
namespace pw::chrono::backend {
int64_t GetSystemClockTickCount() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}
}  // namespace pw::chrono::backend
#endif
"""
    (src_dir / "system_clock_arduino.cpp").write_text(system_clock_cpp, encoding="utf-8")

    # 7.2 Assert backend: abort on assertion failure
    assert_backend_dir = src_dir / "pw_assert_backend"
    assert_backend_dir.mkdir(parents=True, exist_ok=True)
    assert_hdr = """#pragma once
#include <stdlib.h>

#define PW_ASSERT_HANDLE_FAILURE(condition_string) abort()
"""
    check_hdr = """#pragma once
#include <stdlib.h>

#define PW_HANDLE_CRASH(...) abort()
#define PW_HANDLE_ASSERT_FAILURE(condition_string, ...) abort()
#define PW_HANDLE_ASSERT_BINARY_COMPARE_FAILURE(arg_a_str, arg_a_val, comparison_op_str, \
                                                arg_b_str, arg_b_val, type_fmt, message, ...) \
  abort()
"""
    (assert_backend_dir / "assert_backend.h").write_text(assert_hdr, encoding="utf-8")
    (assert_backend_dir / "check_backend.h").write_text(check_hdr, encoding="utf-8")

    # 7.3 Log backend: clean sink callback
    log_backend_dir = src_dir / "pw_log_backend"
    log_backend_dir.mkdir(parents=True, exist_ok=True)
    log_hdr = """#pragma once
#include <stdint.h>
#include <stddef.h>
#include "pw_tokenizer/tokenize.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*pw_log_Sink)(uint8_t level, const uint8_t* log_data, size_t size);
void pw_log_SetLogSink(pw_log_Sink sink);
void pw_log_HandleLog(uint8_t level, const uint8_t* log_data, size_t size);

#ifdef __cplusplus
}
#endif

#define PW_HANDLE_LOG(level, module, flags, message, ...)                      \\
  do {                                                                         \\
    uint8_t _pw_log_buf[64];                                                   \\
    size_t _pw_log_size = sizeof(_pw_log_buf);                                 \\
    PW_TOKENIZE_TO_BUFFER(_pw_log_buf, &_pw_log_size, message, ##__VA_ARGS__); \\
    pw_log_HandleLog((uint8_t)(level), _pw_log_buf, _pw_log_size);             \\
  } while (0)
"""
    (log_backend_dir / "log_backend.h").write_text(log_hdr, encoding="utf-8")

    log_cpp = """#include "pw_log_backend/log_backend.h"

namespace {
pw_log_Sink g_log_sink = nullptr;
}

extern "C" {
void pw_log_SetLogSink(pw_log_Sink sink) {
  g_log_sink = sink;
}

void pw_log_HandleLog(uint8_t level, const uint8_t* log_data, size_t size) {
  if (g_log_sink != nullptr) {
    g_log_sink(level, log_data, size);
  }
}
}
"""
    (src_dir / "log_arduino.cpp").write_text(log_cpp, encoding="utf-8")

    # 6. GCC 7.2.1 Compatibility Adjustments (ARM SAMD toolchain support)
    # Relax constexpr constructors that older GCC versions reject in class hierarchies.
    for rel in (
        "pw_rpc/nanopb/client_reader_writer.h",
        "pw_rpc/nanopb/server_reader_writer.h",
    ):
        hdr = src_dir / rel
        if not hdr.is_file():
            continue
        c = hdr.read_text(encoding="utf-8")
        for old, new in (
            ("constexpr ClientServer(", "ClientServer("),
            (": private internal::NanopbUnaryResponseClientCall<", ": public internal::NanopbUnaryResponseClientCall<"),
            (
                ": private internal::NanopbStreamResponseClientCall<",
                ": public internal::NanopbStreamResponseClientCall<",
            ),
            (": private internal::NanopbServerCall", ": public internal::NanopbServerCall"),
            (": private internal::BaseNanopbServerReader<", ": public internal::BaseNanopbServerReader<"),
            ("constexpr NanopbUnaryResponseClientCall()", "NanopbUnaryResponseClientCall()"),
            ("constexpr NanopbStreamResponseClientCall()", "NanopbStreamResponseClientCall()"),
            ("constexpr NanopbClientReaderWriter() = default;", "NanopbClientReaderWriter() = default;"),
            ("constexpr NanopbClientReader() = default;", "NanopbClientReader() = default;"),
            ("constexpr NanopbClientWriter() = default;", "NanopbClientWriter() = default;"),
            ("constexpr NanopbUnaryReceiver() = default;", "NanopbUnaryReceiver() = default;"),
            ("constexpr NanopbServerCall()", "NanopbServerCall()"),
            ("constexpr BaseNanopbServerReader() = default;", "BaseNanopbServerReader() = default;"),
            ("constexpr NanopbServerReaderWriter() = default;", "NanopbServerReaderWriter() = default;"),
            ("constexpr NanopbServerReader() = default;", "NanopbServerReader() = default;"),
            ("constexpr NanopbServerWriter() = default;", "NanopbServerWriter() = default;"),
            ("constexpr NanopbUnaryResponder() = default;", "NanopbUnaryResponder() = default;"),
            (
                "              DecodeToStructAndInvokeOnCompleted(\n",
                "              this->DecodeToStructAndInvokeOnCompleted(\n",
            ),
            ("          DecodeToStructAndInvokeOnNext(\n", "          this->DecodeToStructAndInvokeOnNext(\n"),
            (
                "                    set_nanopb_on_next_locked(std::move(restored));\n",
                "                    this->set_nanopb_on_next_locked(std::move(restored));\n",
            ),
        ):
            c = c.replace(old, new)
        hdr.write_text(c, encoding="utf-8")

    for rel in (
        "pw_rpc/raw/client_reader_writer.h",
        "pw_rpc/raw/server_reader_writer.h",
        "pw_rpc/internal/client_call.h",
        "pw_rpc/internal/server_call.h",
        "pw_rpc/internal/call.h",
    ):
        hdr = src_dir / rel
        if not hdr.is_file():
            continue
        c = hdr.read_text(encoding="utf-8")
        for old, new in (
            ("constexpr Call()", "Call()"),
            ("constexpr ClientCall() = default;", "ClientCall() = default;"),
            ("constexpr UnaryResponseClientCall() = default;", "UnaryResponseClientCall() = default;"),
            ("constexpr StreamResponseClientCall() = default;", "StreamResponseClientCall() = default;"),
            ("constexpr ServerCall() = default;", "ServerCall() = default;"),
            ("constexpr RawClientReaderWriter() = default;", "RawClientReaderWriter() = default;"),
            ("constexpr RawClientReader() = default;", "RawClientReader() = default;"),
            ("constexpr RawClientWriter() = default;", "RawClientWriter() = default;"),
            ("constexpr RawUnaryReceiver() = default;", "RawUnaryReceiver() = default;"),
            ("constexpr RawServerReaderWriter() = default;", "RawServerReaderWriter() = default;"),
            ("constexpr RawServerReader() = default;", "RawServerReader() = default;"),
            ("constexpr RawServerWriter() = default;", "RawServerWriter() = default;"),
            ("constexpr RawUnaryResponder() = default;", "RawUnaryResponder() = default;"),
            (
                "proto_on_next_local = std::move(proto_on_next);",
                "proto_on_next_local = std::exchange(proto_on_next, nullptr);",
            ),
        ):
            c = c.replace(old, new)
        hdr.write_text(c, encoding="utf-8")

    if (src_dir / "pw_rpc_client.cc").is_file():
        cl_cc = (src_dir / "pw_rpc_client.cc").read_text(encoding="utf-8")
        cl_cc = cl_cc.replace("call == nullptr || call->id() != packet.call_id()", "call == nullptr")
        (src_dir / "pw_rpc_client.cc").write_text(cl_cc, encoding="utf-8")

    print(f"[OK] Mainstream Pigweed Arduino Library exported to {dest_dir}")


cli = typer.Typer(help="Export mainstream Pigweed as an Arduino library for 32-bit targets.", add_completion=False)


@cli.command()
def main(
    target_dir: Annotated[Path, typer.Argument(help="Destination Arduino library directory")] = REPO_ROOT
    / "dist"
    / "Pigweed",
) -> None:
    """Export the pinned upstream Pigweed submodule as the `Pigweed` Arduino library."""
    export_mainstream_library(target_dir)


if __name__ == "__main__":
    cli()
