# Cross-platform agent architecture

## Canonical guidance

GEMINI.md is the canonical repository-wide engineering guide. AGENTS.md, .copilot-instructions, and .agent/rules/openwrt-architect-arduino.agent.md remain Git symlinks to GEMINI.md for tool discovery. The generated Gemini agent profile at .agent/agents/openwrt-architect-arduino/agent.md mirrors that canonical content. The Copilot profile at .github/agents/openwrt-architect-arduino.agent.md is a platform-native adapter: its prompt body is derived from GEMINI.md, but its frontmatter uses Copilot-compatible fields. The instructions field in agent.json is synchronized from GEMINI.md; the Gemini model remains sourced from tools/gemini_model.

## Shared Skills and platform adapters

The canonical shared Skills are discovered through .agents/skills/. In this repository .agents is a Git symlink to .agent, so the actual tracked files live under .agent/skills/ while tools resolve them through the shared .agents/skills/ path. Preserve this symlink; never replace it with a directory. Each Skill is a self-contained directory with SKILL.md and optional references, assets, or scripts. These Skills follow the open Agent Skills format and use concise frontmatter descriptions for automatic matching.

GitHub Copilot's project Skill discovery is supported through .github/skills/. The contents of that directory are generated mirrors of .agents/skills/ and must never be edited independently. The parity auditor checks the contents and metadata of both locations.

| Platform | Persistent guidance | Shared Skills / adaptation |
|---|---|---|
| GitHub Copilot | .github/copilot-instructions.md, root AGENTS.md/GEMINI.md, .github/instructions/*.instructions.md, and .github/agents/*.agent.md | Discovers project Skills in .github/skills/ in supported Copilot surfaces. |
| Gemini CLI | GEMINI.md and hierarchical context files | Discovers workspace Skills in .agents/skills/ or .gemini/skills/. This repository uses .agents/skills/. |
| OpenAI Codex | AGENTS.md and applicable nested AGENTS.md files | Discovers repository Skills in .agents/skills/. |
| ChatGPT desktop | Product guidance and installed Skills | Import individual ZIP archives generated from the canonical Skill directories. Repository presence alone does not install a Skill. |
| ChatGPT Chat/Work | Product/workspace instructions and installed Plugins | Broader web/desktop/mobile distribution can require packaging Skills in a Plugin. Do not claim repository files are automatically discovered in every ChatGPT surface. |

Platform behavior can evolve. Verify current documentation when changing discovery, packaging, or installation instructions.

## Shared Skills

The initial seven cross-platform Skills are:

- ci-failure-diagnosis: investigate and fix failing CI runs and jobs.
- protobuf-protocol-change: modify schemas, generated bindings, RPC, and MCU/MPU protocol contracts.
- embedded-resource-audit: evaluate board-specific firmware, memory, Flash, timing, and emulation.
- openwrt-runtime-debugging: diagnose daemon, procd, UBUS, UCI, LuCI, packages, and networking.
- code-change-verification: select and evaluate tests, static checks, emulation, and full CI evidence.
- security-crypto-review: assess security boundaries, cryptography, secrets, parsing, and attack surfaces.
- code-review: independently review diffs and Pull Requests.

The architect activates only Skills relevant to the requested task. A Skill's presence does not grant tools or permissions. Host permissions, tool availability, and approval boundaries remain authoritative.

## Platform-specific operational workflows

The .agent/workflows/*.md files and .github/commands/*.toml files remain Gemini/GitHub Actions operational prompts. The matching TOML files are generated from canonical workflow Markdown. Existing .agent/skills/gemini-* files remain workflow-specific assets; they are distinct from the new cross-platform engineering Skills.

## Copilot path-specific instructions

Files under .github/instructions/ contain short, path-scoped guidance for Python, firmware, Protobuf, and OpenWrt code. They supplement, rather than duplicate, the repository-wide guide.

## ChatGPT Skill export

Generate one ZIP archive per Skill for manual installation in supported ChatGPT desktop surfaces:

    python tools/export_chatgpt_skills.py

The default output is dist/chatgpt-skills/. To choose another directory:

    python tools/export_chatgpt_skills.py --output /tmp/chatgpt-skills

In ChatGPT desktop, open Skills, choose Create, then Upload from computer, and upload the ZIP for each Skill you want to install. Review the contents before installing. These ZIP files are build artifacts and are not committed to the repository.

For ChatGPT Chat/Work distribution through the web and mobile apps, package the Skills as a Plugin when that distribution is required. Plugin installation/authorization is a separate step and is not implied by this repository's Skill files.

## Architecture decisions

Significant long-lived design choices are recorded as ADRs in docs/architecture/decisions/. Each ADR captures context, decision, considered alternatives, consequences, affected contracts, and migration implications. Routine coding decisions do not require ADRs.

## Synchronization and validation

Generate the Copilot-native agent adapter, Gemini agent profile, agent.json instructions, Copilot Skill mirror, and Gemini command TOML files:

    python tools/audit/check_gemini_parity.py --fix

Run the read-only consistency audit:

    python tools/audit/check_gemini_parity.py
    tox -e agent-parity

The audit validates canonical instructions, generated profiles, shared Skill metadata and mirrors, Copilot agent adaptation, path-specific Copilot instruction discovery, ChatGPT export prerequisites, Gemini model configuration, and Gemini workflow-command pairs.

Each Gemini GitHub Actions workflow reads tools/gemini_model and passes it to the Gemini CLI action.