# Repository instructions for GitHub Copilot

Treat the root GEMINI.md / AGENTS.md as the permanent engineering guide. This file adds Copilot-specific discovery and operating guidance.

## Skill discovery

- Shared Skills are authored canonically in .agents/skills/ and mirrored into .github/skills/ by tools/audit/check_gemini_parity.py.
- Select only the Skills relevant to the user's task. Skill discovery does not mean every Skill must be activated.
- For tasks involving multiple system boundaries, use each relevant Skill and reconcile their findings.
- Skills provide procedures; the root engineering guide and the user's current instructions govern priorities and permissions.

## Repository facts

- Python daemon: mcubridge/
- Cloud gateway: mcubridge-gateway/
- Client examples: mcubridge-client-examples/
- Arduino library and firmware: mcubridge-library-arduino/
- LuCI application: luci-app-mcubridge/
- Canonical protocol schemas: tools/protocol/config_schema.proto, tools/protocol/mcu_wire.proto, and tools/protocol/cloud_gateway.proto.
- Inspect pyproject.toml, requirements/, tox.ini, generator scripts, and the relevant CI workflow instead of assuming versions or commands.

## Change discipline

- Inspect relevant source, callers, tests, generated outputs, and configuration before editing.
- Preserve public APIs and wire contracts unless the user explicitly requests a change; for protocol changes, trace both MCU and MPU sides and all generated/client consumers.
- Prefer direct invocation of suitable existing library APIs over hand-written equivalents or forwarding wrappers. Recommend useful libraries proactively, but do not add a new external dependency without explicit user approval.
- Be proactive about nearby inconsistencies, security problems, duplicate logic, unused code, and improvement opportunities. Report out-of-scope findings separately rather than silently broadening the patch.
- Localized, low-risk fixes may be implemented directly. Ask for approval before high-impact redesigns, breaking changes, destructive migrations, or broad refactors.
- Add meaningful regression tests. Run focused checks first and complete CI before treating a PR as validated.
- Report what was actually tested, including unavailable hardware and unexecuted checks.

## GitHub permissions

Work on a dedicated branch; do not commit or push directly to main. Create commits and PRs as appropriate. Never merge into main without the user's explicit authorization.