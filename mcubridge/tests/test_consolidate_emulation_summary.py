"""Unit tests for tools/ci/consolidate_emulation_summary.py (SIL-2 / Rule 11 / Rule 37)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.ci import consolidate_emulation_summary


def test_generate_consolidated_markdown_empty(tmp_path: Path) -> None:
    s_dir = tmp_path / "simavr"
    r_dir = tmp_path / "renode"
    s_dir.mkdir()
    r_dir.mkdir()

    content = consolidate_emulation_summary.generate_consolidated_markdown(s_dir, r_dir)
    assert "Multi-Architecture Hardware Emulation Matrix" in content
    assert "No hardware emulation data found" in content


def test_generate_consolidated_markdown_from_json(tmp_path: Path) -> None:
    s_dir = tmp_path / "simavr"
    r_dir = tmp_path / "renode"
    q_dir = tmp_path / "qemu"
    s_dir.mkdir()
    r_dir.mkdir()
    q_dir.mkdir()

    (s_dir / "simavr_summary.json").write_text(
        json.dumps(
            {
                "engine": "simavr (Cycle-Accurate)",
                "boards": [
                    {
                        "board_fqbn": "arduino:avr:mega",
                        "board_name": "Arduino Mega 2560 (ATmega2560)",
                        "architecture": "AVR 8-bit",
                        "compilation": "✅ Compiled",
                        "emulation": "✅ Passed (100% E2E)",
                        "result": "✅ PASS",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    (r_dir / "renode_summary.json").write_text(
        json.dumps(
            {
                "engine": "Renode (Instruction-Accurate)",
                "boards": [
                    {
                        "board_fqbn": "arduino:samd:mkrwifi1010",
                        "board_name": "Arduino MKR WiFi 1010 (ATSAMD21G18A)",
                        "architecture": "ARM Cortex-M0+",
                        "compilation": "✅ Compiled",
                        "emulation": "✅ Passed (100% E2E)",
                        "result": "✅ PASS",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    (q_dir / "qemu_summary.json").write_text(
        json.dumps(
            {
                "engine": "QEMU Espressif (Instruction-Accurate)",
                "boards": [
                    {
                        "board_fqbn": "arduino:esp32:nano_nora",
                        "board_name": "Arduino Nano ESP32 (ESP32-S3)",
                        "architecture": "Xtensa LX7 (ESP32-S3)",
                        "compilation": "✅ Compiled",
                        "emulation": "✅ Passed (100% E2E)",
                        "result": "✅ PASS",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    content = consolidate_emulation_summary.generate_consolidated_markdown(s_dir, r_dir, qemu_dir=q_dir)
    assert "Arduino Mega 2560" in content
    assert "arduino:avr:mega" in content
    assert "simavr (Cycle-Accurate)" in content
    assert "Arduino MKR WiFi 1010" in content
    assert "arduino:samd:mkrwifi1010" in content
    assert "Renode (Instruction-Accurate)" in content
    assert "Arduino Nano ESP32" in content
    assert "arduino:esp32:nano_nora" in content
    assert "QEMU Espressif (Instruction-Accurate)" in content
    assert "✅ PASS" in content


def test_generate_consolidated_markdown_fallback_md(tmp_path: Path) -> None:
    s_dir = tmp_path / "simavr"
    r_dir = tmp_path / "renode"
    q_dir = tmp_path / "qemu"
    s_dir.mkdir()
    r_dir.mkdir()
    q_dir.mkdir()

    sample_md = (
        "### simavr\n\n"
        "| Board | Arch | Comp | Emu | Result |\n"
        "| :--- | :---: | :---: | :---: | :---: |\n"
        "| **Arduino Mega 2560 (ATmega2560)**<br>`arduino:avr:mega` | AVR 8-bit | ✅ Compiled | "
        "✅ Passed (100% E2E) | **✅ PASS** |\n"
    )
    (s_dir / "simavr_summary.md").write_text(sample_md, encoding="utf-8")

    qemu_md = (
        "### QEMU Xtensa\n\n"
        "| Board | Arch | Comp | Emu | Result |\n"
        "| :--- | :---: | :---: | :---: | :---: |\n"
        "| **Arduino Nano ESP32 (ESP32-S3)**<br>`arduino:esp32:nano_nora` | Xtensa LX7 (ESP32-S3) | ✅ Compiled | "
        "✅ Passed (100% E2E) | **✅ PASS** |\n"
    )
    (q_dir / "qemu_summary.md").write_text(qemu_md, encoding="utf-8")

    content = consolidate_emulation_summary.generate_consolidated_markdown(s_dir, r_dir, qemu_dir=q_dir)
    assert "Arduino Mega 2560" in content
    assert "AVR 8-bit" in content
    assert "Arduino Nano ESP32" in content
    assert "Xtensa LX7 (ESP32-S3)" in content
    assert "✅ PASS" in content


def test_main_cli_writes_output_and_step_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    s_dir = tmp_path / "simavr"
    r_dir = tmp_path / "renode"
    q_dir = tmp_path / "qemu"
    s_dir.mkdir()
    r_dir.mkdir()
    q_dir.mkdir()

    out_file = tmp_path / "unified.md"
    step_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_file))

    consolidate_emulation_summary.main(
        simavr_dir=s_dir,
        renode_dir=r_dir,
        qemu_dir=q_dir,
        output=out_file,
        step_summary=True,
    )

    assert out_file.exists()
    assert step_file.exists()
    assert out_file.read_text(encoding="utf-8") == step_file.read_text(encoding="utf-8")
