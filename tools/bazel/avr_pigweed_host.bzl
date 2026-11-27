"""Repository rule exposing AVR Pigweed port modules for Bazel host tests.

Firmware builds (AVR, SAMD21, ESP32, and the native emulator) compile:
  1. Upstream Pigweed pw_transfer patched with avr_pigweed_port's cooperative
     execution adaptation (PW_TRANSFER_COOPERATIVE=1, TransferThread::Poll(),
     and CooperativeHandlerRegistration).
  2. avr_pigweed_port's tokenized logging backend (pw_log_avr), which routes
     PW_LOG_* invocations through pw_tokenizer to Bridge.sendLogStream().

Bazel host tests must link these exact implementations instead of upstream
threaded @pigweed//pw_transfer and plain-text @pigweed//pw_log_basic so host
tests exercise the same transfer engine and tokenized logging path shipped to
devices without mutating the pinned git submodules.
"""

_PW_TRANSFER_SOURCES = [
    "chunk.cc",
    "client.cc",
    "client_context.cc",
    "context.cc",
    "rate_estimate.cc",
    "server_context.cc",
    "transfer.cc",
    "transfer_thread.cc",
]

_PATCH = "third_party/avr_pigweed_port/patches/0001-avr-pw-transfer-and-protobuf.patch"
_REGISTRATION_HDR = (
    "third_party/avr_pigweed_port/pw_transfer_avr/public/pw_transfer/" +
    "cooperative_handler_registration.h"
)

_BUILD = """\
load("@rules_cc//cc:defs.bzl", "cc_library")

package(default_visibility = ["//visibility:public"])

cc_library(
    name = "pw_transfer",
    srcs = {transfer_srcs},
    hdrs = glob(
        ["pw_transfer/public/pw_transfer/**/*.h"],
        exclude = ["pw_transfer/public/pw_transfer/internal/config.h"],
    ),
    strip_include_prefix = "pw_transfer/public",
    # Single source of truth for the cooperative configuration so every
    # translation unit including pw_transfer headers agrees on the layout of
    # TransferThread (ODR). Values match PW_COOPERATIVE_DEFINES in
    # tools/ci/arduino_toolchains.sh.
    defines = [
        "PW_TRANSFER_COOPERATIVE=1",
        "PW_TRANSFER_COOPERATIVE_QUEUE_CAPACITY=2",
        "PW_TRANSFER_COOPERATIVE_CHUNK_BUFFER_SIZE=128",
    ],
    deps = [
        "@pigweed//pw_assert:assert",
        "@pigweed//pw_assert:check",
        "@pigweed//pw_bytes",
        "@pigweed//pw_chrono:system_clock",
        "@pigweed//pw_containers:intrusive_forward_list",
        "@pigweed//pw_function",
        "@pigweed//pw_log",
        "@pigweed//pw_log:rate_limited",
        "@pigweed//pw_preprocessor",
        "@pigweed//pw_protobuf",
        "@pigweed//pw_result",
        "@pigweed//pw_rpc:client_server",
        "@pigweed//pw_rpc:internal_packet_pwpb",
        "@pigweed//pw_rpc/raw:client_api",
        "@pigweed//pw_rpc/raw:server_api",
        "@pigweed//pw_span",
        "@pigweed//pw_status",
        "@pigweed//pw_stream",
        "@pigweed//pw_sync:no_lock",
        "@pigweed//pw_transfer:config",
        "@pigweed//pw_transfer:transfer_pwpb",
        "@pigweed//pw_transfer:transfer_raw_rpc",
        "@pigweed//pw_varint",
    ],
)

cc_library(
    name = "pw_log_avr",
    srcs = ["pw_log_avr/src/log_avr.cpp"],
    hdrs = [
        "pw_log_avr/public/pw_log_avr/log_avr.h",
        "pw_log_avr/public_overrides/pw_log_backend/log_backend.h",
    ],
    includes = [
        "pw_log_avr/public",
        "pw_log_avr/public_overrides",
    ],
    deps = [
        "@pigweed//pw_log:pw_log.facade",
        "@pigweed//pw_tokenizer",
    ],
)

cc_library(
    name = "pw_log_avr_impl",
)
"""

def _copy_tree(rctx, src, dest):
    stack = [(src, dest)]
    for _ in range(10000):
        if not stack:
            break
        cur_src, cur_dest = stack.pop()
        for entry in cur_src.readdir():
            target = cur_dest + "/" + entry.basename
            if entry.is_dir:
                stack.append((entry, target))
            else:
                rctx.file(target, rctx.read(entry), executable = False)

def _pw_transfer_hunks(patch_text):
    blocks = patch_text.split("\ndiff --git ")
    kept = []
    for index, block in enumerate(blocks):
        body = block if index == 0 else "diff --git " + block
        if body.startswith("diff --git a/pw_transfer/"):
            kept.append(body)
    if not kept:
        fail("No pw_transfer hunks found in " + _PATCH)
    return "\n".join(kept) + "\n"

def _child_path(root, rel):
    cur = root
    for part in rel.split("/"):
        cur = cur.get_child(part)
    return cur

def _avr_pigweed_host_impl(rctx):
    root = rctx.path(Label("@@//:MODULE.bazel")).dirname
    upstream_transfer = _child_path(root, "third_party/pigweed/pw_transfer")
    if not upstream_transfer.exists:
        fail("third_party/pigweed is not checked out; run `git submodule update --init third_party/pigweed`.")

    for name in _PW_TRANSFER_SOURCES:
        rctx.file("pw_transfer/" + name, rctx.read(upstream_transfer.get_child(name)))
    _copy_tree(rctx, upstream_transfer.get_child("public"), "pw_transfer/public")

    patch_path = _child_path(root, _PATCH)
    rctx.file("pw_transfer.patch", _pw_transfer_hunks(rctx.read(patch_path)))
    rctx.patch("pw_transfer.patch", strip = 1)
    rctx.delete("pw_transfer.patch")

    registration = _child_path(root, _REGISTRATION_HDR)
    rctx.file(
        "pw_transfer/public/pw_transfer/cooperative_handler_registration.h",
        rctx.read(registration),
    )

    log_avr_root = _child_path(root, "third_party/avr_pigweed_port/pw_log_avr")
    if not log_avr_root.exists:
        fail("third_party/avr_pigweed_port is not checked out; run `git submodule update --init third_party/avr_pigweed_port`.")
    _copy_tree(rctx, log_avr_root.get_child("public"), "pw_log_avr/public")
    _copy_tree(rctx, log_avr_root.get_child("public_overrides"), "pw_log_avr/public_overrides")
    rctx.file(
        "pw_log_avr/src/log_avr.cpp",
        rctx.read(_child_path(log_avr_root, "src/log_avr.cpp")),
    )

    rctx.file("BUILD.bazel", _BUILD.format(
        transfer_srcs = repr(["pw_transfer/" + name for name in _PW_TRANSFER_SOURCES]),
    ))

avr_pigweed_host = repository_rule(
    implementation = _avr_pigweed_host_impl,
    doc = "Cooperative pw_transfer and tokenized pw_log_avr matching the MCU firmware build.",
)
