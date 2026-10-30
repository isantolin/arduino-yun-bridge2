---
name: openwrt-runtime-debugging
description: Diagnose OpenWrt runtime, procd services, UBUS/UCI IPC, LuCI, networking, package deployment, or MPU daemon behavior.
---

# OpenWrt Runtime Debugging

Establish actual OpenWrt release/build, target architecture, package format, installed package versions, init/service definition, runtime configuration, process topology, and whether the target is physical hardware, QEMU/libvirt, or another emulator.

## Diagnostic sequence

1. Inspect source, package manifests, init scripts, UCI defaults, LuCI RPC calls, and deployment workflow.
2. Check service state, procd/logread output, process lifecycle, paths, permissions, sockets, interfaces, routes, and firewall boundaries.
3. Trace the request across LuCI/HTTP, UBUS, daemon asyncio loop, serial transport, MCU, and gateway as applicable.
4. Compare configured values with effective runtime values. Do not assume localhost, interface binding, device path, or UCI defaults.
5. Verify UBUS signatures, argument policies, session injection, return types, and frontend error handling at the actual boundary.
6. Inspect daemon status snapshots and health counters for integration/live-runtime work.
7. Separate build, installation, boot, service, configuration, IPC, network, and hardware failures.

Prefer native UBUS/UCI/procd/LuCI and existing library APIs. Do not add wrappers around direct API calls without a real domain/security/lifecycle/compatibility reason. Treat RPC arguments, paths, process requests, and remote commands as untrusted. Preserve least privilege, explicit errors, atomic persistence, and safe defaults. Avoid intrusive/destructive commands without authorization.

Use actual tox, package build, emulation, and deployment scripts. State whether validation occurred in a VM, emulator, or physical device. Include logs, status evidence, network path, package version, and limitations.