---
name: openwrt-runtime-debugging
description: Diagnose OpenWrt runtime problems involving services, procd, UBUS, UCI, LuCI, daemon lifecycle, IPC, networking, or package deployment.
---

# OpenWrt Runtime Debugging

1. Identify OpenWrt release, target architecture, package format, installed package versions, init scripts, service names, and active configuration. Verify on target or from authoritative manifests.
2. Distinguish build-time configuration from effective UCI values and running process state.
3. Inspect relevant procd status, service logs, daemon health snapshots, sockets, permissions, filesystem paths, and process ownership.
4. For host/MPU/MCU or VM boundaries, trace listen addresses, routes, firewall zones, ports, DNS, serial mapping, and runtime configuration. Never assume localhost crosses a VM or host boundary.
5. For UBUS/LuCI, inspect method signatures, policies, session injection, JSON-RPC response shapes, and frontend error handling. Use snapshot fallback only when part of the actual contract.
6. Avoid reading /proc/<pid>/fd/* contents; character devices, sockets, and pipes can block. Inspect descriptor symlink metadata or use lsof/fuser when available.
7. Prefer OpenWrt-native APIs, ubus/UCI interfaces, and existing language libraries over hand-written protocol or shell plumbing when suitable. Do not wrap a native API unless the wrapper adds validation, lifecycle, security, or domain behavior.
8. Verify installed package/Python API versions; do not assume upstream features exist in an APK or on the target ABI.
9. Reproduce using the narrowest safe method. Run relevant package build, LuCI, daemon, integration, and emulation tests.
10. Distinguish host tests, emulation, and physical device tests. Do not modify a live device or perform a destructive install/upgrade without explicit authorization.

## Completion report

Report release/target, runtime facts, relevant logs/status, root cause, changes, validation, and device-only steps not executed.
