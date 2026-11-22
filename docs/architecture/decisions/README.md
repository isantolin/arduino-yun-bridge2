# Architecture Decision Records

Record decisions that materially affect repository architecture, public contracts, security boundaries, dependencies, or long-term maintenance.

## Status values

- Proposed: under discussion; not yet approved.
- Accepted: approved and in force.
- Superseded: replaced by a later ADR.
- Deprecated: no longer recommended but retained for historical context.

## Required structure

1. Title, status, date, and decision owners.
2. Context and problem statement.
3. Decision and rationale.
4. Alternatives considered and why they were not selected.
5. Consequences, risks, affected components, and migration/rollback notes.
6. Related ADRs and implementation references.

Create ADRs for significant decisions, not routine implementation details.

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](0001-cross-platform-agent-skills.md) | Cross-platform engineering agent and shared Skills | Accepted |
| [ADR-0002](0002-pigweed-hdlc-rpc-migration.md) | Migration of MCU-MPU Serial Transport and RPC to Google Pigweed | Accepted |
| [ADR-0003](0003-pigweed-gateway-replacement.md) | Reemplazo integral del Gateway por Pigweed | Accepted |
| [ADR-0004](0004-linux-mpu-storage-ssot.md) | Filesystem de Linux MPU como SSOT de Almacenamiento Común | Accepted (amended by ADR-0007, ADR-0008) |
| [ADR-0005](0005-mcu-contributed-handshake-nonce.md) | MCU-Contributed Nonce in the Link Handshake | Accepted |
| [ADR-0006](0006-wolfcrypt-link-crypto.md) | WolfCrypt Link Crypto Provider | Accepted (amended) |
| [ADR-0007](0007-cloud-eeprom-access-and-persistent-storage.md) | Cloud Access to the AVR EEPROM and Persistent Linux Storage | Accepted (completed by ADR-0008) |
| [ADR-0008](0008-retire-unary-filesystem-write.md) | Retire Unary FileSystemService.Write in Favor of pw_transfer | Accepted |
