"""Unit tests for tools/profiling/arduino_symbol_profiler.py (SIL-2 / Rule 17)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from tools.profiling.arduino_symbol_profiler import (
    cli,
    detect_board_label,
    extract_elf_memory_table,
    extract_symbols,
    profile_elf,
)

runner = CliRunner()


def test_detect_board_label() -> None:
    build_dir = Path("/tmp/build")
    elf_path = Path("/tmp/build/arduino-avr-mega/BridgeControl/BridgeControl.ino.elf")
    label = detect_board_label(build_dir, elf_path)
    assert label == "Arduino Mega 2560"

    mkr_elf = Path("/tmp/build/arduino-samd-mkrwifi1010/BridgeControl/BridgeControl.ino.elf")
    assert detect_board_label(build_dir, mkr_elf) == "Arduino MKR WiFi 1010"

    nano_elf = Path("/tmp/build/arduino-esp32-nano_nora/BridgeControl/BridgeControl.ino.elf")
    assert detect_board_label(build_dir, nano_elf) == "Arduino Nano ESP32"

    generic_path = Path("/tmp/build/generic_board/firmware.elf")
    label_generic = detect_board_label(build_dir, generic_path)
    assert label_generic == "generic_board"


def test_extract_symbols_real_elf() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    elf_path = repo_root / "arduino-build" / "arduino-avr-mega" / "BridgeControl" / "BridgeControl.ino.elf"
    if not elf_path.exists():
        pytest.skip(f"Build artifact not found: {elf_path}")

    symbols = extract_symbols(elf_path, limit=5)
    assert len(symbols) > 0
    assert len(symbols) <= 5
    assert all("B" in sym for sym in symbols)
    assert any("Bridge" in sym or "main" in sym or "poly" in sym for sym in symbols)


def test_extract_symbols_invalid_elf(tmp_path: Path) -> None:
    bad_elf = tmp_path / "corrupt.elf"
    bad_elf.write_bytes(b"not an elf file header")
    symbols = extract_symbols(bad_elf)
    assert symbols == []


def test_profile_elf_valid_and_invalid(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    # 1. Invalid / empty file
    bad_elf = build_dir / "empty.elf"
    bad_elf.write_bytes(b"")
    output = profile_elf(build_dir, bad_elf)
    assert "Symbol Profiling (pyelftools)" in output
    assert "No symbols found or file unreadable" in output

    # 2. Real ELF if available
    repo_root = Path(__file__).resolve().parents[2]
    real_elf = repo_root / "arduino-build" / "arduino-avr-mega" / "BridgeControl" / "BridgeControl.ino.elf"
    if real_elf.exists():
        real_output = profile_elf(repo_root / "arduino-build", real_elf)
        assert "Symbol Profiling (pyelftools)" in real_output
        assert "BridgeControl.ino.elf" in real_output
        assert "B" in real_output


def test_cli_main_nonexistent_directory(tmp_path: Path) -> None:
    result = runner.invoke(cast(Any, cli), [str(tmp_path / "does_not_exist")])
    assert result.exit_code == 0
    assert "not found" in result.output


def test_cli_main_with_output(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    candidate_dirs = [
        repo_root / "arduino-build" / "arduino-avr-mega",
        repo_root / "arduino-build" / "arduino-samd-mkrwifi1010",
        repo_root / "arduino-build" / "arduino-esp32-nano_nora",
    ]
    build_dir = next((d for d in candidate_dirs if d.exists() and list(d.rglob("*.elf"))), None)
    if build_dir is None:
        pytest.skip("No build artifacts found for supported target boards (Mega, MKR WiFi 1010, Nano ESP32)")

    out_file = tmp_path / "report.md"
    result = runner.invoke(cast(Any, cli), [str(build_dir), "--output", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "Symbol Profiling (pyelftools)" in content


def test_extract_elf_memory_table() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    build_dir = repo_root / "arduino-build" / "arduino-avr-mega"
    if not build_dir.exists():
        pytest.skip(f"Build directory not found: {build_dir}")

    table = extract_elf_memory_table(build_dir)
    assert table is not None
    assert "### 📊 Arduino Memory Usage" in table
    assert "Arduino Mega 2560" in table
    assert "`BridgeControl`" in table
