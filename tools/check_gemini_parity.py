#!/usr/bin/env python3
"""Validate parity between .agent/workflows/*.md and .github/commands/*.toml,
and verify agent.json model matches the canonical tools/gemini_model file."""

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent

WORKFLOWS = [
    "gemini-invoke",
    "gemini-plan-execute",
    "gemini-review",
    "gemini-scheduled-triage",
    "gemini-triage",
]


def _parse_md(path: Path) -> tuple[str, str]:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return "", text.strip()
    fm, body = m.group(1), m.group(2)
    desc_m = re.search(r"description:\s*(.+)", fm)
    desc = desc_m.group(1).strip() if desc_m else ""
    return desc, body.strip()


def check_model_parity() -> list[str]:
    canonical_path = ROOT / "tools" / "gemini_model"
    canonical = canonical_path.read_text().strip()
    agent_path = ROOT / ".agent" / "agents" / "openwrt-architect-arduino" / "agent.json"
    agent = json.loads(agent_path.read_text())
    actual = agent.get("model", "")
    if actual != canonical:
        return [f"agent.json model '{actual}' != canonical '{canonical}' " f"(source: tools/gemini_model)"]
    return []


def check_prompt_parity() -> list[str]:
    errors: list[str] = []
    for name in WORKFLOWS:
        md_path = ROOT / ".agent" / "workflows" / f"{name}.md"
        toml_path = ROOT / ".github" / "commands" / f"{name}.toml"

        for p in (md_path, toml_path):
            if not p.exists():
                errors.append(f"Missing file: {p}")

        if not md_path.exists() or not toml_path.exists():
            continue

        md_desc, md_prompt = _parse_md(md_path)
        with open(toml_path, "rb") as f:
            toml_data = tomllib.load(f)
        toml_desc = toml_data.get("description", "")
        toml_prompt = toml_data.get("prompt", "").strip()

        if md_desc != toml_desc:
            errors.append(
                f"{name}: description mismatch\n"
                f"  .md  description: '{md_desc}'\n"
                f"  .toml description: '{toml_desc}'"
            )

        if md_prompt != toml_prompt:
            md_lines = md_prompt.splitlines()
            toml_lines = toml_prompt.splitlines()
            detail = f"line counts differ: md={len(md_lines)}, toml={len(toml_lines)}"
            for i, (l1, l2) in enumerate(zip(md_lines, toml_lines)):
                if l1 != l2:
                    detail = f"first diff at line {i + 1}:\n" f"  .md  : {l1[:120]!r}\n" f"  .toml: {l2[:120]!r}"
                    break
            errors.append(f"{name}: prompt mismatch ({detail})")

    return errors


if __name__ == "__main__":
    errors = check_model_parity() + check_prompt_parity()
    if errors:
        print("PARITY FAILURES:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print(f"All parity checks passed ({len(WORKFLOWS)} workflow pairs + model).")
