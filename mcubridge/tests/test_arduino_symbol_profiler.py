"""Unit tests for tools/profiling/arduino_symbol_profiler.py (SIL-2 / Rule 17)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

from typer.testing import CliRunner

from tools.profiling.arduino_symbol_profiler import (
    cli,
    detect_board_label,
    extract_elf_memory_table,
    extract_symbols,
    profile_elf,
)

runner = CliRunner()


def ensure_test_elf(target_path: Path) -> Path:
    """Ensure a syntactically valid ELF file exists at target_path for pyelftools inspection."""
    if target_path.exists():
        return target_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    compiler = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    if compiler:
        code = (
            b"int my_ram_var = 123; "
            b"int my_poly_bridge_func(void) { return my_ram_var; } "
            b"int main(void) { return my_poly_bridge_func(); }"
        )
        subprocess.run(
            [compiler, "-x", "c", "-", "-o", str(target_path), "-nostdlib", "-Wl,-e,0"],
            input=code,
            check=True,
        )
    return target_path


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


def test_extract_symbols_real_elf(tmp_path: Path) -> None:
    elf_path = ensure_test_elf(tmp_path / "arduino-avr-mega" / "BridgeControl" / "BridgeControl.ino.elf")
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

    # 2. Real ELF file
    real_elf = ensure_test_elf(tmp_path / "arduino-avr-mega" / "BridgeControl" / "BridgeControl.ino.elf")
    real_output = profile_elf(tmp_path / "arduino-avr-mega", real_elf)
    assert "Symbol Profiling (pyelftools)" in real_output
    assert "BridgeControl.ino.elf" in real_output
    assert "B" in real_output


def test_cli_main_nonexistent_directory(tmp_path: Path) -> None:
    result = runner.invoke(cast(Any, cli), [str(tmp_path / "does_not_exist")])
    assert result.exit_code == 0
    assert "not found" in result.output


def test_cli_main_with_output(tmp_path: Path) -> None:
    build_dir = tmp_path / "arduino-avr-mega"
    ensure_test_elf(build_dir / "BridgeControl" / "BridgeControl.ino.elf")

    out_file = tmp_path / "report.md"
    result = runner.invoke(cast(Any, cli), [str(build_dir), "--output", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "Symbol Profiling (pyelftools)" in content


def test_extract_elf_memory_table(tmp_path: Path) -> None:
    build_dir = tmp_path / "arduino-avr-mega"
    ensure_test_elf(build_dir / "BridgeControl" / "BridgeControl.ino.elf")

    table = extract_elf_memory_table(build_dir)
    assert table is not None
    assert "### 📊 Arduino Memory Usage" in table
    assert "Arduino Mega 2560" in table
    assert "`BridgeControl`" in table


def test_extract_elf_memory_table_esp32(tmp_path: Path) -> None:
    build_dir = tmp_path / "arduino-esp32-nano_nora"
    ensure_test_elf(build_dir / "BridgeControl" / "BridgeControl.ino.elf")

    table = extract_elf_memory_table(build_dir)
    assert table is not None
    assert "### 📊 Arduino Memory Usage" in table
    assert "Arduino Nano ESP32" in table
    assert "`BridgeControl`" in table
