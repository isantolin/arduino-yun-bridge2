# Bridge repository line census

## Scope and reproducibility

- Repository: `isantolin/arduino-yun-bridge2`
- Snapshot: `50f68f93582cec5e0c7edfe8bc3d8db1bad15486`
- Pull request: #102 (`feat/pigweed-resource-accounting`)
- Method: enumerate the recursive Git tree, fetch every tracked blob, and count physical text lines. A trailing newline is not counted as an additional empty line; empty files count as zero.
- Result: **426 tracked blobs, 74,306 physical lines**.
- The tree also contains two submodule gitlinks. Their contents are not included in the 426-blob census:
  - `third_party/pigweed` at `55873baa1b37eaf7d0a830515a0846e3fc3a79e2` (upstream Pigweed)
  - `third_party/avr_pigweed_port` at `ee9708e34606c0775a433aa003e5006b50c456b0` (separately maintained AVR port)

## Census by repository area

The 297 source-like files (C/C++, Python, scripts, JavaScript, Protobuf, CI/workflow definitions, templates and type stubs) account for 64,843 physical lines.

| Area (path-based classification) | Files | Physical lines |
|---|---:|---:|
| CI/workflows, root scripts and LuCI | 28 | 5,301 |
| Client examples and C++ gateway | 24 | 5,795 |
| Arduino library and Arduino tests | 60 | 11,135 |
| Daemon, services and client scripts | 24 | 6,818 |
| Python tests | 76 | 16,289 |
| Tooling, CI scripts and emulation | 68 | 15,990 |
| Protobuf, protocol tooling and type stubs | 17 | 3,515 |
| **Source-like subtotal** | **297** | **64,843** |

The remaining 129 tracked blobs (documentation, agent instructions, configuration, package recipes, translations, build metadata and templates/configuration not classified as source-like above) account for **9,463 physical lines**.

| Census | Files | Physical lines |
|---|---:|---:|
| Source-like files | 297 | 64,843 |
| Documentation/configuration/other tracked files | 129 | 9,463 |
| **Repository total** | **426** | **74,306** |

## Change-volume context (not Pigweed attribution)

Two comparisons provide context but must not be interpreted as the net cost or savings of Pigweed:

1. From the parent of PR #57 (`f1e7a5a4a92f06a17b946ac23cbe066987e35ce6`) to this snapshot: 882 commits, 300 changed files, +9,489 / -6,805 lines, net +2,684. This interval contains unrelated maintenance, CI, documentation, security and other project work.
2. PR #102 base (`a39f51a5a531f0c5769ecc0199548a4434d0a0ff`) to its current head: 106 commits, 29 changed files, +611 / -126, net +485. This PR primarily adds resource-accounting/reporting and CI changes; it is not a pure Pigweed migration diff.

A valid Pigweed-only net LOC figure requires a reviewed set of migration commits/PRs and classification of each changed line as migration implementation, removed legacy implementation, generated code, upstream vendoring, tests, or unrelated maintenance. The broad historical comparison above is deliberately not used as a substitute.

## Interpretation and limitations

- These are **physical lines**, not logical/productive LOC. Blank lines and comments are included.
- The area classification is based on repository paths, not a semantic review of every function.
- Generated files, test code, documentation, scripts and production code are not interchangeable measures of implementation complexity.
- Upstream Pigweed and the AVR port are separate submodules and are excluded from the Bridge repository total. Counting their upstream source would overwhelm the application-code measure and would misattribute upstream code to this project.
- No line-count result proves memory, flash, runtime, or maintenance savings. Those require the target resource reports and a like-for-like before/after implementation comparison.
- The source census is exhaustive for tracked blobs at the stated snapshot; it does not include untracked build outputs, submodule contents, or files absent from Git.

## Next measurement needed for a strict productive-LOC audit

Run language-aware counting on a local checkout of this exact SHA to split physical lines into code, comments and blanks (Python tokenization; C/C++ lexical parsing; Protobuf/JSON/YAML/shell-aware comment handling). Preserve generated files and tests as separate categories. Then compare the same file sets at the pre-migration baseline and post-migration snapshot, excluding upstream submodule content and unrelated PRs. Until that is done, report physical lines only and do not claim a Pigweed LOC reduction.
