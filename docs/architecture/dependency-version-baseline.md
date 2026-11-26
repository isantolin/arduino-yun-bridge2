# Dependency version baseline

Audit date: 2026-11-26

This document distinguishes version streams that must not be forced to the same
number (for example, Protobuf's Bazel module version and its Python wheel
version). The source-of-truth manifests remain `MODULE.bazel`,
`requirements/runtime.toml`, `.bazelversion`, and the pinned submodules.

## Verified pins

| Component | Pin | Scope / status |
|---|---:|---|
| Pigweed source | `55873baa1b37eaf7d0a830515a0846e3fc3a79e2` | Exact source revision used by the AVR port |
| Bazel | `9.3.3` | Latest stable version selected for this migration; Bazel 10 is not selected |
| Bazelisk | `1.30.0` | Explicitly pinned in CI |
| setup-bazel action | `0.19.2` | Explicitly pinned in CI |
| Pigweed Bzlmod module | `0.0.1` | Pigweed source module version; source identity is the Git SHA above |
| AVR port Bzlmod module | `0.1.0` | Local module version |
| Protobuf (Bzlmod/C++) | `36.2` | Separate from the Python Protobuf wheel |
| Protobuf (Python) | `5.28.3` | Retained for the currently published Pigweed Python wheel |
| protoc | `28.3` | Matches the Python Protobuf 5.28.x generation toolchain |
| rules_cc | `0.2.28` | Bzlmod |
| platforms | `1.1.0` | Bzlmod |
| rules_oci | `2.3.5` | Bzlmod |
| rules_pkg | `1.3.0` | Bzlmod |
| Nanopb generator (PyPI) | `0.4.9.3` | Latest stable PyPI generator package |
| Nanopb C runtime | `0.4.9.2` | Latest upstream C runtime tag; distinct release stream from PyPI generator |
| ETL | `20.49.0` | Latest upstream release tag checked |
| Unity | `2.7.0` | Latest upstream release tag checked |
| wolfSSL Arduino wrapper | `5.8.4` | Latest release of the Arduino-specific wrapper |
| FlashStorage | `1.0.0` | Current pinned upstream release |
| HTTPX | `0.29.0` | Latest stable PyPI release as of this audit |
| Espressif Arduino core | `4.0.0` | Latest stable release; based on ESP-IDF 6.1.1 |

## Compatibility boundaries that remain open

### Pigweed Python wheel versus Pigweed source

The repository uses Pigweed source at
`55873baa1b37eaf7d0a830515a0846e3fc3a79e2`, while the latest published
`pigweed` package on PyPI is still `0.0.24` (published 2025-06-11).
That wheel constrains Python Protobuf to the 5.28.x line. It is not a release
of the current source tree and must not be represented as version-aligned with
the source SHA.

The durable resolution is to package the required Pigweed Python modules from
the pinned source revision (and use that package in OpenWrt/CI), or to obtain
a matching upstream PyPI release. Do not upgrade Python Protobuf independently
while retaining the old wheel.

### wolfSSL Arduino wrapper versus wolfSSL core

The latest upstream wolfSSL core release is 5.9.4, but the Arduino-specific
wrapper's latest published tag remains 5.8.4. The current dependency is pinned
to the wrapper, not directly to the core. Do not change its source URL/tag to
5.9.4 without first validating that the Arduino wrapper structure, user
settings, and Mega/ESP32 builds are preserved. This remains a security and
integration review item.

### Bazel lockfile

`MODULE.bazel.lock` was previously excluded by `.gitignore`. That exclusion
has been removed in the Bridge branch. The lockfile still needs to be generated
by Bazel 9.3.3 and committed after the full Bzlmod graph resolves. Until then,
the manifest pins are explicit but the transitive Bazel graph is not fully
locked.

### Bridge-to-AVR-port submodule gate

The Bridge consumes the AVR port through a submodule and a local Bzlmod
override. The required peripheral Bazel targets are currently in AVR port
PR #27. The Bridge dependency audit also reports its checked-out AVR port
gitlink as behind the latest `main` revision. After PR #27 is reviewed and
merged, update the Bridge gitlink to that merged stable `main` commit and
rerun `deps-sync`. Do not point a stable submodule reference at an unmerged
feature branch merely to suppress the audit.

## Reproducibility requirements

- Keep the exact Pigweed source SHA visible and identical in the AVR port and
  Bridge nested source checkouts.
- Keep Bzlmod dependency versions in `MODULE.bazel`; commit the generated
  `MODULE.bazel.lock`.
- Keep Python pins in `requirements/runtime.toml` and update all bootstrap,
  tox, packaging, and CI installation commands from that source of truth.
- Keep Nanopb's Python generator and C runtime versions separately recorded.
- Do not silently bump the Arduino wolfSSL wrapper beyond its latest published
  wrapper release.
- Run the full Bridge and AVR port CI after every toolchain or dependency
  change. CI success on host/simavr is not physical Yún UART/HDLC validation.

## Upstream references

- [Pigweed Bazel integration](https://pigweed.dev/build/bazel/integration/setup.html)
- [Bazel Central Registry: rules_oci](https://registry.bazel.build/modules/rules_oci/)
- [Bazel Central Registry: rules_pkg](https://registry.bazel.build/modules/rules_pkg/)
- [Nanopb releases](https://github.com/nanopb/nanopb/releases)
- [Nanopb PyPI releases](https://pypi.org/project/nanopb/)
- [wolfSSL core releases](https://github.com/wolfSSL/wolfssl/releases)
- [Arduino-wolfSSL releases](https://github.com/wolfSSL/Arduino-wolfSSL/releases)
- [Espressif Arduino core releases](https://github.com/espressif/arduino-esp32/releases)
