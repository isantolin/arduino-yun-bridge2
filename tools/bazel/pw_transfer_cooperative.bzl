"""Hermetic Bazel repository rule for cooperative pw_transfer from avr_pigweed_port."""

_BUILD_FILE_TEMPLATE = """load("@com_google_protobuf//bazel:proto_library.bzl", "proto_library")
load("@pigweed//pw_protobuf_compiler:pw_proto_filegroup.bzl", "pw_proto_filegroup")
load("@pigweed//pw_protobuf_compiler:pwpb_proto_library.bzl", "pwpb_proto_library")
load("@pigweed//pw_protobuf_compiler:raw_rpc_proto_library.bzl", "raw_rpc_proto_library")
load("@rules_cc//cc:defs.bzl", "cc_library")

{proto_section}

pwpb_proto_library(
    name = "transfer_pwpb",
    deps = [":transfer_proto"],
)

raw_rpc_proto_library(
    name = "transfer_raw_rpc",
    deps = [":transfer_proto"],
)

cc_library(
    name = "pw_transfer_cooperative",
    srcs = glob(
        ["pw_transfer/*.cc"],
        exclude = [
            "pw_transfer/*_test.cc",
            "pw_transfer/*_fuzzer.cc",
            "pw_transfer/fuzz_*.cc",
            "pw_transfer/test_*.cc",
            "pw_transfer/integration_test*.cc",
            "pw_transfer/atomic_file_transfer_handler.cc",
            "pw_transfer/java_jni*.cc",
        ],
    ),
    hdrs = glob(
        ["pw_transfer/public/**/*.h"],
        exclude = [
            "pw_transfer/public/pw_transfer/atomic_file_transfer_handler.h",
        ],
    ),
    defines = [
        "PW_TRANSFER_COOPERATIVE=1",
        "PW_TRANSFER_COOPERATIVE_QUEUE_CAPACITY=2",
        "PW_TRANSFER_COOPERATIVE_CHUNK_BUFFER_SIZE=128",
    ],
    includes = ["pw_transfer/public"],
    visibility = ["//visibility:public"],
    deps = [
        ":transfer_pwpb",
        ":transfer_raw_rpc",
        "@pigweed//pw_assert:assert",
        "@pigweed//pw_assert:check",
        "@pigweed//pw_bytes",
        "@pigweed//pw_chrono:system_clock",
        "@pigweed//pw_containers:intrusive_list",
        "@pigweed//pw_function",
        "@pigweed//pw_log",
        "@pigweed//pw_protobuf",
        "@pigweed//pw_result",
        "@pigweed//pw_rpc",
        "@pigweed//pw_rpc/raw:client_api",
        "@pigweed//pw_rpc/raw:server_api",
        "@pigweed//pw_span",
        "@pigweed//pw_status",
        "@pigweed//pw_stream",
        "@pigweed//pw_sync:binary_semaphore",
        "@pigweed//pw_sync:mutex",
        "@pigweed//pw_sync:timed_thread_notification",
        "@pigweed//pw_thread:thread",
        "@pigweed//pw_varint",
    ],
)
"""

_PATCH_SCRIPT = """
import pathlib, shutil, subprocess, sys

pigweed_dir = pathlib.Path(sys.argv[1])
avr_port_dir = pathlib.Path(sys.argv[2])
out_dir = pathlib.Path(sys.argv[3])

dst_transfer = out_dir / "pw_transfer"
shutil.copytree(pigweed_dir / "pw_transfer", dst_transfer)

# Remove nested BUILD/BUILD.bazel files so root glob() does not hit subpackages.
for b in list(dst_transfer.rglob("BUILD")) + list(dst_transfer.rglob("BUILD.bazel")):
    if b.is_file():
        b.unlink()

patch_file = avr_port_dir / "patches" / "0001-avr-pw-transfer-and-protobuf.patch"
text = patch_file.read_text(encoding="utf-8")
parts = text.split("diff --git ")
filtered = []
for part in parts[1:]:
    first_line = part.splitlines()[0]
    if first_line.startswith("a/pw_transfer/"):
        filtered.append("diff --git " + part)

if filtered:
    patch_input = "".join(filtered)
    res = subprocess.run(
        ["patch", "-p1", "--batch", "--no-backup-if-mismatch"],
        input=patch_input,
        text=True,
        cwd=out_dir,
        capture_output=True,
    )
    if res.returncode != 0:
        sys.stderr.write(res.stdout + "\\n" + res.stderr + "\\n")
        sys.exit(res.returncode)

coop_hdr = avr_port_dir / "pw_transfer_avr" / "public" / "pw_transfer" / "cooperative_handler_registration.h"
shutil.copy2(coop_hdr, dst_transfer / "public" / "pw_transfer" / "cooperative_handler_registration.h")
"""

def _pw_transfer_cooperative_impl(rctx):
    pigweed_dir = rctx.path(rctx.attr.pigweed_module).dirname
    avr_port_dir = rctx.path(rctx.attr.avr_patch).dirname.dirname
    res = rctx.execute([
        "python3",
        "-c",
        _PATCH_SCRIPT,
        str(pigweed_dir),
        str(avr_port_dir),
        str(rctx.path(".")),
    ])
    if res.return_code != 0:
        fail("Failed to stage cooperative pw_transfer:\n{}\n{}".format(res.stdout, res.stderr))

    if rctx.path("pw_transfer/transfer.pwpb_options").exists:
        proto_section = """pw_proto_filegroup(
    name = "transfer_proto_and_options",
    srcs = ["pw_transfer/transfer.proto"],
    options_files = ["pw_transfer/transfer.pwpb_options"],
)

proto_library(
    name = "transfer_proto",
    srcs = [":transfer_proto_and_options"],
)"""
    else:
        proto_section = """proto_library(
    name = "transfer_proto",
    srcs = ["pw_transfer/transfer.proto"],
)"""

    rctx.file("BUILD.bazel", _BUILD_FILE_TEMPLATE.format(proto_section = proto_section))

pw_transfer_cooperative_repo = repository_rule(
    implementation = _pw_transfer_cooperative_impl,
    attrs = {
        "pigweed_module": attr.label(mandatory = True),
        "avr_patch": attr.label(mandatory = True),
    },
    local = True,
)
