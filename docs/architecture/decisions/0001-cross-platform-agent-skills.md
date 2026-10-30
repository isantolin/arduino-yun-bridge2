# ADR-0001: Cross-platform engineering agent and shared Skills

- Status: Accepted
- Date: 2026-10-30
- Decision owners: Repository maintainer

## Context

The repository had a large, globally applied agent prompt containing architectural rules, implementation preferences, test procedures, hardware-specific requirements, and Gemini/GitHub Actions operations. This increased context load, created contradictory mandates, and did not provide a consistent reusable Skill layout across GitHub Copilot, Gemini CLI, OpenAI Codex, and ChatGPT.

## Decision

1. Keep one concise repository-wide architecture guide in GEMINI.md, exposed through the existing AGENTS.md and compatibility symlinks.
2. Use a central architect agent to understand impact, select only relevant Skills, coordinate specialists when helpful, and preserve user approval boundaries.
3. Store seven shared task Skills canonically under .agents/skills/: CI diagnosis, Protobuf protocol changes, embedded resource audits, OpenWrt runtime debugging, code-change verification, security/cryptography review, and code review.
4. Generate a byte-for-byte mirror of those Skills in .github/skills/ for supported Copilot discovery surfaces.
5. Keep existing Gemini/Antigravity GitHub Actions workflows and their operational Skill assets separate from the shared engineering Skills.
6. Use Copilot path-scoped instructions for Python, firmware, Protobuf, and OpenWrt code.
7. Make Library-First and direct library invocation permanent principles. Compare overlapping libraries based on total system complexity, correctness, security, resource cost, compatibility, and maintenance.
8. Require explicit user approval before adding, installing, vendoring, or wiring a new external dependency into the project or CI.
9. Apply proactive engineering intelligence proportionally: investigate the affected system, expand based on impact, and report unrelated findings separately rather than silently broadening the patch.
10. Preserve user control over high-impact changes, destructive operations, and GitHub merges. Never modify main directly or merge without explicit user authorization.
11. Require evidence-based validation tied to the exact commit SHA, with explicit separation between tests, emulation, and physical hardware.
12. Record significant future architecture decisions as ADRs and report strategic improvement opportunities separately from the active task.

## Alternatives considered

- One universal prompt containing every procedure: rejected because global instructions become long, contradictory, and expensive for unrelated tasks.
- Separate duplicated Skills for every AI platform: rejected because the same procedure would drift across tools.
- One shared .agents/skills/ directory only: insufficient for Copilot discovery in supported surfaces; a generated .github/skills/ mirror is maintained and audited.
- Automatic addition of recommended dependencies: rejected because dependency selection has supply-chain, compatibility, maintenance, and resource consequences requiring maintainer approval.
- Global repository audit on every request: rejected because unnecessary scans consume time and context and can cause scope expansion.

## Consequences

- The permanent prompt is shorter and contains stable project-wide rules.
- Detailed procedures are loaded on demand and can include references, scripts, and examples.
- Copilot-specific mirrors and instructions must be regenerated and checked whenever canonical Skills or agent guidance change.
- ChatGPT web/Work availability may require packaging the standalone Skills in a Plugin; repository presence alone does not guarantee automatic discovery in every ChatGPT surface.
- Existing Gemini Actions workflows remain independent operational workflows and continue to use their existing model configuration.
- The parity audit becomes a cross-platform agent configuration gate.

## Migration and rollback

- Existing root instruction symlinks are preserved.
- Previous Gemini-specific operational Skills and workflows remain in place.
- New shared Skills and Copilot mirror are additive.
- Reverting this ADR's implementation restores the previous generated agent profile and checker state without changing application runtime or protocol behavior.