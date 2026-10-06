"""Test suite for tools/audit/check_gemini_parity.py. [SIL-2]"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from tools.audit.check_gemini_parity import (
    app,
    check_model_parity,
    check_prompt_parity,
    check_rules_parity,
    sync_rules_to_agent_json,
)

runner = CliRunner()


def test_check_model_parity_success() -> None:
    errors = check_model_parity()
    assert errors == []


def test_check_prompt_parity_success() -> None:
    errors = check_prompt_parity()
    assert errors == []


def test_check_rules_parity_success() -> None:
    errors = check_rules_parity()
    assert errors == []


def test_check_rules_parity_preserves_copilot_specific_guidance(mocker: MockerFixture, tmp_path: Path) -> None:
    canonical = "Canonical Rules Content"
    (tmp_path / "GEMINI.md").write_text(canonical, encoding="utf-8")
    mirror_paths = (
        tmp_path / "AGENTS.md",
        tmp_path / ".copilot-instructions",
        tmp_path / ".agent" / "rules" / "openwrt-architect-arduino.agent.md",
        tmp_path / ".github" / "agents" / "openwrt-architect-arduino.agent.md",
    )
    for mirror_path in mirror_paths:
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(canonical, encoding="utf-8")

    agent_path = tmp_path / ".agent" / "agents" / "openwrt-architect-arduino" / "agent.json"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text(json.dumps({"instructions": canonical}), encoding="utf-8")

    copilot_paths = (
        tmp_path / ".github" / "copilot-instructions",
        tmp_path / ".github" / "copilot-instructions.md",
    )
    for copilot_path in copilot_paths:
        copilot_path.write_text("Copilot-specific guidance", encoding="utf-8")

    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)

    assert check_rules_parity() == []
    assert [path.read_text(encoding="utf-8") for path in copilot_paths] == [
        "Copilot-specific guidance",
        "Copilot-specific guidance",
    ]


def test_sync_rules_to_agent_json_success(mocker: MockerFixture, tmp_path: Path) -> None:
    mock_gemini = tmp_path / "GEMINI.md"
    mock_gemini.write_text("Canonical Rules Content\n", encoding="utf-8")

    agent_dir = tmp_path / ".agent" / "agents" / "openwrt-architect-arduino"
    agent_dir.mkdir(parents=True)
    agent_json = agent_dir / "agent.json"
    agent_json.write_text(json.dumps({"name": "test", "instructions": "old"}), encoding="utf-8")

    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    res = sync_rules_to_agent_json()
    assert res is True
    data = json.loads(agent_json.read_text(encoding="utf-8"))
    assert data["instructions"] == "Canonical Rules Content"


def test_sync_rules_to_agent_json_missing_files(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    assert sync_rules_to_agent_json() is False

    mock_gemini = tmp_path / "GEMINI.md"
    mock_gemini.write_text("Rules", encoding="utf-8")
    assert sync_rules_to_agent_json() is False


def test_cli_main_and_fix() -> None:
    res = runner.invoke(cast(Any, app), ["--fix"])
    assert res.exit_code == 0
    assert "All parity checks passed" in res.stdout
