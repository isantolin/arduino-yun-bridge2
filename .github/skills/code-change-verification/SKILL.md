---
name: code-change-verification
description: Validate code modifications, refactors, tests, generated files, type checks, lint, packaging, emulation, or Pull Request readiness.
---

# Code Change Verification

1. Read acceptance criteria; identify changed files, public contracts, generated outputs, affected components, and risk.
2. Select the narrowest tests that genuinely validate behavior. Add regression tests for new defects.
3. Run focused tests and static checks first. Use tox.ini, pyproject.toml, generator scripts, and CI workflows as source of truth.
4. Expand by impact: Python, C++, protocol, LuCI, packaging, security, emulation, or hardware checks.
5. Before treating a PR as ready, obtain successful full CI for the exact PR head SHA and inspect individual jobs. Re-run after code changes that invalidate evidence.
6. Verify generated artifacts against canonical sources. Do not edit generated staging output instead of its generator.
7. Distinguish passed, failed, skipped, xfailed, unavailable, and unexecuted checks.
8. Do not weaken assertions, lower coverage thresholds, suppress diagnostics, or mock primary behavior to pass.

Report each relevant check and exact SHA. For embedded work identify board/target and emulator or physical hardware. For protocol work confirm both directions and generated outputs. For runtime work distinguish unit tests from integration/live-device checks. A queued workflow or prior green SHA does not validate the current PR.