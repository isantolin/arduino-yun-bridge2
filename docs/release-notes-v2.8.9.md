# MCU Bridge 2.8.9 — Release Notes (draft)

**Status:** Draft for PR #48. Do not publish until the PR is merged and the complete CI, emulation, and packaging checks pass.

## Highlights

- Reduce event-loop blocking by moving LMDB transactions, compaction, lifecycle operations, and filesystem work to worker threads.
- Cache persistent deque length so queue-depth reads are O(1), while serializing deque mutations and lifecycle operations.
- Stream local file transfers to the MCU in bounded chunks and preserve the protocol's empty end-of-file frame.
- Accumulate MCU file-read chunks in a single `bytearray` and materialize the unary gRPC response once.
- Reduce serial/Protobuf frame parsing copies with `memoryview`; retain bounded cryptographic key caching and remove the high-cardinality sequence-ID AAD cache.
- Use `asyncio.timeout` and `asyncio.TaskGroup` for runtime deadlines and serial task supervision.
- Correct transport capability reporting: the current grpclib implementation uses HTTP/2; an HTTP/3 request warns and falls back to HTTP/2 instead of reporting a false QUIC connection.

## Compatibility and migration

- No serial wire-protocol change is intended.
- The unary gRPC `FileRead` response contract remains unchanged.
- HTTP/3 is not implemented; deployments requesting it continue over HTTP/2 with an explicit warning.
- No configuration migration is expected from these changes.

## Validation status

- CI for PR #48 is still running; earlier runs exposed type-checking, test-integrity, and branch-coverage issues.
- No performance percentages are claimed. Any throughput or latency claims require reproducible benchmarks on representative hardware.
- Validate file-transfer behavior and runtime stability on a physical Arduino Yún / YúnShield before declaring the release production-ready.

## Release procedure

1. Merge PR #48 only after all required CI checks pass.
2. Confirm package metadata and runtime `__version__` both report `2.8.9`.
3. Create tag `v2.8.9` on the merged commit.
4. The `Publish Python Package to GitHub Packages` workflow builds the source distribution and wheel and attaches them to the GitHub Release.
5. Verify release assets and checksums before announcing the release.
