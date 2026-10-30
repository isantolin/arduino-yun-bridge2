---
name: code-review
description: Independently review changed code or a GitHub Pull Request for correctness, security, protocol, resources, testing, and maintainability before merge.
---

# Independent Code Review

1. Inspect PR metadata, base/head SHAs, changed files, complete diff, and surrounding implementation. Confirm the diff matches the intended change.
2. Review changed lines, then trace callers, consumers, contracts, generated code, config, and tests as needed.
3. Prioritize correctness, security, integrity, concurrency, protocol compatibility, hardware/resource limits, and regressions.
4. Apply Library-First: identify manual code duplicated by existing APIs, unnecessary wrappers, and avoidable dependencies. Recommend only concrete improvements.
5. Inspect test quality, boundaries, regression coverage, and whether mocks bypass behavior.
6. Check CI evidence and exact SHA. A green workflow from an older commit does not validate this PR.
7. Report actionable findings only. Do not invent issues, request generic checks, or repeat what the code already proves.
8. Keep unrelated architectural ideas in a separate improvement section.

For each finding include severity (critical/high/medium/low), exact file and diff line(s), evidence, impact, recommendation, and confidence (confirmed/probable/needs verification). Do not approve, request changes, or merge unless explicitly requested and permitted by the active review workflow. A review recommendation is not formal approval.

Summarize scope, strengths, blocking findings, non-blocking improvements, CI status, and unverified areas.