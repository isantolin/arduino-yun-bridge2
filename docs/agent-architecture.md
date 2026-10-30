# Agent architecture and source-of-truth

## Canonical rules

`GEMINI.md` is the single source of truth for repository-wide architectural agent instructions.

The following compatibility paths are Git symlinks to the canonical file:

| Path | Link target |
|---|---|
| `AGENTS.md` | `GEMINI.md` |
| `.copilot-instructions` | `GEMINI.md` |
| `.agent/rules/openwrt-architect-arduino.agent.md` | `../../GEMINI.md` |

Tool-specific discovery files remain regular files because Gemini/Antigravity and GitHub Copilot discover agents through different paths. They are generated from `GEMINI.md`:

- `.agent/agents/openwrt-architect-arduino/agent.md`
- `.github/agents/openwrt-architect-arduino.agent.md`

`agent.json` retains the agent name, description, model and other metadata. Its `instructions` field is synchronized from `GEMINI.md`.

## Operational profiles

The five operational profiles have separate responsibilities and are not duplicate agents:

| Profile | Responsibility |
|---|---|
| `gemini-invoke` | Analyze an issue and publish a maintainer-approval plan. |
| `gemini-plan-execute` | Execute the latest approved plan. |
| `gemini-review` | Review changed lines in a pull request. |
| `gemini-triage` | Classify one issue using existing labels. |
| `gemini-scheduled-triage` | Classify an issue batch on a schedule. |

The workflow Markdown files in `.agent/workflows/` are canonical for command prompts. Their matching `.github/commands/*.toml` files are generated adapters. Skills and GitHub Actions workflow YAML remain separate because they define different execution and integration behavior.

`.github/copilot-instructions.md` is intentionally retained because it contains Copilot-specific guidance and must not be replaced with the architectural prompt. The redundant `.github/copilot-instructions` copy is removed.

## Synchronization and validation

Regenerate agent discovery profiles, `agent.json` instructions and command TOML files:

```bash
python tools/audit/check_gemini_parity.py --fix
```

Run the read-only consistency audit:

```bash
python tools/audit/check_gemini_parity.py
tox -e gemini-parity
```

GitHub Actions additionally checks that the repository variable `vars.GEMINI_MODEL` matches `tools/gemini_model`. Configure both to the same model identifier. This environment check is a CI-only requirement; a local parity check does not require access to repository variables.
