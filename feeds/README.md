# Local McuBridge feed

This directory contains two kinds of entries:

- Versioned Python package recipes (for example, `python3-protobuf`,
  `python3-pigweed`, and `python3-cryptography`). These Makefiles are
  maintained in Git and are built as packages by the OpenWrt SDK.
- The three application packages (`mcubridge`, `mcubridge-gateway`, and
  `luci-app-mcubridge`) are canonicalized at the repository root. During a
  build, `tools/ci/sync_feed_overlay.py` creates symlinks for those packages
  so the SDK consumes the source without copying it.

Only the application-package symlinks are generated. Do not delete the
versioned Python recipe directories when cleaning the feed overlay.
