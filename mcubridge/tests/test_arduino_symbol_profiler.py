"""Unit tests for tools/profiling/arduino_symbol_profiler.py (SIL-2 / Rule 17)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from elftools.common.exceptions import ELFError
from typer.testing import CliRunner

from tools.profiling.arduino_symbol_profiler import (
    cli,
    detect_board_label,
    extract_symbols,
    parse_memory_logs,
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


def test_parse_memory_logs(tmp_path: Path) -> None:
    # 1. Non-existent directory returns None
    assert parse_memory_logs(tmp_path / "non_existent") is None

    # 2. Empty directory returns None
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    assert parse_memory_logs(log_dir) is None

    # 3. Directory with valid log returns markdown table
    log_file = log_dir / "arduino-samd-mkrwifi1010__BridgeControl.log"
    log_file.write_text(
        "Sketch uses 12345 bytes (42%) of program storage space. Maximum is 28672 bytes.\n"
        "Global variables use 1024 bytes (40%) of dynamic memory. Maximum is 2560 bytes.\n",
        encoding="utf-8",
    )
    nano_log = log_dir / "arduino-esp32-nano_nora__BridgeWiFi.log"
    nano_log.write_text(
        "El Sketch usa 12345 bytes (42%) del espacio de almacenamiento de programa. "
        "El máximo es 3145728 bytes.\n"
        "Las variables Globales usan 1024 bytes (1%) de la memoria dinámica, "
        "dejando 326656 bytes para las variables locales. El máximo es 327680 bytes.\n",
        encoding="utf-8",
    )
    result = parse_memory_logs(log_dir)
    assert result is not None
    assert "### 📊 Arduino Memory Usage" in result
    assert "Arduino MKR WiFi 1010" in result
    assert "Arduino Nano ESP32" in result
    assert "3,145,728 B" in result
    assert "327,680 B" in result
    assert "`BridgeControl`" in result
    assert "`BridgeWiFi`" in result
    assert "12,345 / 28,672 B" in result
    assert "1,024 / 2,560 B" in result


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
    with pytest.raises(ELFError):
        extract_symbols(bad_elf)


def test_profile_elf_valid_and_invalid(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    # 1. Invalid / empty file
    bad_elf = build_dir / "empty.elf"
    bad_elf.write_bytes(b"")
    with pytest.raises(ELFError):
        profile_elf(build_dir, bad_elf)

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
    assert result.exit_code != 0
    assert "not found" in result.output


def test_cli_main_with_output(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    build_dir = repo_root / "arduino-build" / "arduino-avr-yun"
    if not build_dir.exists():
        pytest.skip(f"Build dir not found: {build_dir}")

    out_file = tmp_path / "report.md"
    result = runner.invoke(cast(Any, cli), [str(build_dir), "--output", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "Symbol Profiling (pyelftools)" in content
