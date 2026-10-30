---
name: security-crypto-review
description: Review cryptography, authentication, secrets, protocol parsing, network boundaries, IPC, remote commands, file/process access, or security-sensitive CI configuration.
---

# Security and Cryptography Review

1. Identify assets, trust boundaries, attacker-controlled inputs, privileges, interfaces, and failure consequences.
2. Trace validation from ingress to use. Check lengths/ranges, parsing, authorization, identity binding, paths, process execution, and error paths.
3. Inspect secret storage/logging, defaults, credentials, transport security, replay defense, nonce lifecycle, key lifetime, and cryptographic API usage.
4. Search adjacent callers for the same weakness. Distinguish confirmed vulnerabilities from hypotheses needing runtime or threat-model evidence.
5. Use maintained cryptographic libraries and public APIs. Never implement cryptographic primitives, random generators, AEAD, signatures, or key derivation manually.
6. Evaluate advisories, versions, configuration, platform support, and build options. New dependencies require explicit user approval.
7. Use configured Semgrep, static audits, protocol fuzzing, and negative/boundary tests where relevant. Inspect tool configuration first.

A known-answer test or self-test is not FIPS certification or an independent security assessment. Never expose secrets. Do not weaken validation or silently downgrade security when an API fails. Consider error oracles, timing, resource exhaustion, nonce reuse/exhaustion, and downgrade paths. Escalate critical findings immediately; keep unrelated remediation separate.

Report severity, boundary, evidence, impact/exploitability, confidence, recommended fix, and remaining validation.