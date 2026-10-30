---
name: ci-failure-diagnosis
description: Diagnose and fix failing GitHub Actions or CI jobs. Use when a workflow run, test job, lint, typecheck, build, emulation, or release pipeline fails.
---

# CI Failure Diagnosis

1. Identify repository, branch, commit SHA, workflow run, attempt, and failed job. Confirm that the run tested the intended SHA.
2. Inspect failed-job logs and the relevant workflow, composite actions, scripts, dependency manifests, and test configuration.
3. Find the first causal error; separate it from downstream failures, cleanup, retries, and summaries.
4. Trace the source. Compare the tested file at that SHA with the current branch; account for generated files, formatter/linter rewrites, environment, caches, and tool versions.
5. Reproduce with the narrowest relevant local command or tox environment. Do not assume local and CI environments are identical.
6. Implement a focused correction and add a regression test when a missed defect is exposed.
7. Run the focused check, then broader relevant checks. Push only to the authorized branch and inspect the new run and each job.
8. Report final SHA, run URL, job conclusions, and checks not run.

Do not blindly rerun deterministic failures. Do not silence warnings or weaken tests to pass CI. Check whether CI formatters/generators mutate files before later steps. Distinguish product defects, test defects, workflow errors, dependency/toolchain drift, and infrastructure failures. Never claim physical hardware validation from emulator jobs.

Inspect .github/workflows/ci.yml, .github/actions/, tox.ini, pyproject.toml, requirements/, tools/ci/, and tools/audit/. These files define current validation commands.