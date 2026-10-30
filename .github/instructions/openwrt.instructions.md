---
applyTo: "mcubridge/**/*.py,mcubridge-gateway/**/*.py,luci-app-mcubridge/**/*,mcubridge/scripts/**/*"
---

Verify the actual OpenWrt release, target architecture, runtime configuration, service definition, package metadata, and IPC contract before changing behavior. Trace relevant calls through procd, UBUS/UCI, asyncio daemon, serial transport, LuCI, and gateway. Prefer native OpenWrt and existing project-library APIs. Preserve validation, least privilege, atomic persistence, explicit errors, and correct session handling. Use actual runtime or emulator evidence; do not assume localhost, device names, or active configuration.