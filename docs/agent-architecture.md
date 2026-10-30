# Agent architecture and source-of-truth

## Canonical rules

`GEMINI.md` is the single source of truth for repository-wide architectural agent instructions.

The following compatibility paths are Git symlinks to the canonical file:

| Path | Link target |
|---|---|
| `AGENTS.md` | `GEMINI.md` |
| `.copilot-instructions` | `GEMINI.md` |
| `.agent/rules/openwrt-architect-arduino.agent.md` | `../../GEMINI.md` |

Tool-specific discovery files remain regular files because Gemini and GitHub Copilot use different agent frontmatter and discovery conventions:

- `.agent/agents/openwrt-architect-arduino/agent.md` is generated as the Gemini agent profile and mirrors the canonical guide.
- `.github/agents/openwrt-architect-arduino.agent.md` is generated as a Copilot-native adapter. Its prompt body is derived from `GEMINI.md`, while its frontmatter uses Copilot-compatible `name`, `description`, and `tools` fields.

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

Regenerate the Copilot-native agent adapter, Gemini agent profile, `agent.json` instructions, Copilot Skill mirror, and command TOML files:

```bash
python tools/audit/check_gemini_parity.py --fix
```

Run the read-only consistency audit:

```bash
python tools/audit/check_gemini_parity.py
tox -e agent-parity
```

Each Gemini GitHub Actions workflow checks out the repository, reads `tools/gemini_model`, and passes that value to the Gemini CLI action. This keeps workflow execution aligned with the canonical model file and removes a separately maintained repository variable.
