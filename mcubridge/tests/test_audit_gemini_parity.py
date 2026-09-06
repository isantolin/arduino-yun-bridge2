"""Test suite for tools/audit/check_gemini_parity.py. [SIL-2]"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

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


def test_sync_rules_to_agent_json_success(tmp_path: Path) -> None:
    mock_gemini = tmp_path / "GEMINI.md"
    mock_gemini.write_text("Canonical Rules Content\n", encoding="utf-8")

    agent_dir = tmp_path / ".agent" / "agents" / "openwrt-architect-arduino"
    agent_dir.mkdir(parents=True)
    agent_json = agent_dir / "agent.json"
    agent_json.write_text(json.dumps({"name": "test", "instructions": "old"}), encoding="utf-8")

    with patch("tools.audit.check_gemini_parity.ROOT", tmp_path):
        res = sync_rules_to_agent_json()
        assert res is True
        data = json.loads(agent_json.read_text(encoding="utf-8"))
        assert data["instructions"] == "Canonical Rules Content"


def test_sync_rules_to_agent_json_missing_files(tmp_path: Path) -> None:
    with patch("tools.audit.check_gemini_parity.ROOT", tmp_path):
        assert sync_rules_to_agent_json() is False

        mock_gemini = tmp_path / "GEMINI.md"
        mock_gemini.write_text("Rules", encoding="utf-8")
        assert sync_rules_to_agent_json() is False


def test_cli_main_and_fix() -> None:
    res = runner.invoke(cast(Any, app), ["--fix"])
    assert res.exit_code == 0
    assert "All parity checks passed" in res.stdout
