"""Unit tests for coverage_python tool (SIL-2 / Rule 11 / Rule 18)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.ci import coverage_python


def test_coverage_python_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_dir = tmp_path / "coverage"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "coverage.json"

    mock_pytest_main = MagicMock(return_value=0)
    monkeypatch.setattr(coverage_python.pytest, "main", mock_pytest_main)

    class DummyCoverage:
        def __init__(self, data_file: str) -> None:
            self.data_file = data_file

        def load(self) -> None:
            pass

        def json_report(self, outfile: str) -> None:
            Path(outfile).write_text(
                json.dumps(
                    {
                        "totals": {
                            "percent_branches_covered": 96.5,
                            "num_branches": 100,
                            "covered_branches": 96,
                        }
                    }
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr(coverage_python.coverage, "Coverage", DummyCoverage)

    coverage_python.main(
        output_root=output_dir,
        html=True,
        emit_json=True,
        min_coverage=95.0,
        min_branch=95.0,
        pytest_args=["mcubridge/tests"],
    )

    assert mock_pytest_main.called
    assert json_path.exists()
    assert "percent_branches_covered" in json_path.read_text(encoding="utf-8")


def test_coverage_python_pytest_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_dir = tmp_path / "coverage"
    output_dir.mkdir(parents=True, exist_ok=True)

    mock_pytest_main = MagicMock(return_value=2)
    monkeypatch.setattr(coverage_python.pytest, "main", mock_pytest_main)

    with pytest.raises(SystemExit) as exc_info:
        coverage_python.main(output_root=output_dir, pytest_args=["tests"])

    assert exc_info.value.code == 2


def test_coverage_python_branch_coverage_under_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "coverage"
    output_dir.mkdir(parents=True, exist_ok=True)

    mock_pytest_main = MagicMock(return_value=0)
    monkeypatch.setattr(coverage_python.pytest, "main", mock_pytest_main)

    class DummyCoverage:
        def __init__(self, data_file: str) -> None:
            self.data_file = data_file

        def load(self) -> None:
            pass

        def json_report(self, outfile: str) -> None:
            Path(outfile).write_text(
                json.dumps(
                    {
                        "totals": {
                            "percent_branches_covered": 88.0,
                            "num_branches": 100,
                            "covered_branches": 88,
                        }
                    }
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr(coverage_python.coverage, "Coverage", DummyCoverage)

    with pytest.raises(SystemExit) as exc_info:
        coverage_python.main(
            output_root=output_dir,
            min_branch=95.0,
        )

    assert exc_info.value.code == 1


def test_coverage_python_missing_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_dir = tmp_path / "coverage"
    output_dir.mkdir(parents=True, exist_ok=True)

    mock_pytest_main = MagicMock(return_value=0)
    monkeypatch.setattr(coverage_python.pytest, "main", mock_pytest_main)

    class DummyCoverage:
        def __init__(self, data_file: str) -> None:
            self.data_file = data_file

        def load(self) -> None:
            pass

        def json_report(self, outfile: str) -> None:
            # Does not write file
            pass

    monkeypatch.setattr(coverage_python.coverage, "Coverage", DummyCoverage)

    with pytest.raises(SystemExit) as exc_info:
        coverage_python.main(output_root=output_dir)

    assert exc_info.value.code == 1
