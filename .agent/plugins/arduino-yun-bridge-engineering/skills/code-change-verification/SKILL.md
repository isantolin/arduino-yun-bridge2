---
name: code-change-verification
description: Verify changes with appropriate tests, type checks, linters, audits, integration tests, emulation, and complete CI evidence.
---

# Code Change Verification

1. Inspect the diff and determine affected languages, components, interfaces, generated artifacts, and deployment targets.
2. Read tox.ini, pyproject.toml, build manifests, and component docs. Use configured commands and pinned versions as the source of truth.
3. Establish a focused baseline and run narrow relevant checks first. Do not run every expensive audit for trivial localized/documentation changes.
4. Select tests based on impact: Python, C++, protocol integrity, LuCI, static analysis, typing, package builds, emulation, security scans, or runtime health audits.
5. Distinguish regressions from pre-existing failures and infrastructure issues using baseline, logs, and commit SHA.

## Test integrity

- Assert observable behavior: return values, state changes, emitted frames, persisted data, errors, logs, or external effects.
- Reject tautologies, line-hitting tests, discarded results, self-mutation assertions, and tests that bypass the system under test.
- Use test doubles at justified boundaries; do not mock the primary unit under test.
- Do not weaken tests, thresholds, or diagnostics to make checks pass. Add regression tests for missed defects.

## CI readiness

- Before marking a PR ready, inspect the full applicable CI suite and latest workflow attempt.
- Verify required jobs and confirm the run's head SHA matches the latest PR head SHA.
- Record skipped, cancelled, missing, or permission-blocked jobs.
- An older green run does not validate a newer commit.
- Passing CI is evidence for readiness, not authorization to merge.

## Evidence report

For every check record whether it was not run, blocked, failed, or passed. Name emulators/targets. Confirm physical hardware use before claiming hardware validation. Exact WCET, SRAM, Flash, coverage, and performance claims require actual measured output.
