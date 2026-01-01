from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _iter_text_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(root.rglob(pattern))
    return sorted({path for path in files if path.is_file()})


def _find_matches(files: list[Path], regex: re.Pattern[str]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # C++ sources should be UTF-8; if not, ignore rather than flake.
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append((path, idx, line.strip()))
    return hits


def test_no_print_in_yunbridge_runtime() -> None:
    """Keep daemon runtime logs structured; allow prints only in tools."""

    yunbridge_root = _REPO_ROOT / "openwrt-yun-bridge" / "yunbridge"
    assert yunbridge_root.is_dir(), f"missing yunbridge root: {yunbridge_root}"

    # We allow prints in CLI/debug helpers under yunbridge/tools.
    excluded_prefixes = {
        yunbridge_root / "tools",
    }

    py_files = _iter_text_files(yunbridge_root, ("*.py",))
    py_files = [
        path
        for path in py_files
        if not any(path.is_relative_to(prefix) for prefix in excluded_prefixes)
    ]

    print_regex = re.compile(r"\bprint\s*\(")
    hits = _find_matches(py_files, print_regex)

    assert not hits, "print() is not allowed in yunbridge runtime modules:\n" + "\n".join(
        f"{path.relative_to(_REPO_ROOT)}:{line_no}: {line}" for path, line_no, line in hits
    )


def test_no_stl_in_mcu_runtime_src() -> None:
    """MCU runtime must stay C++11-friendly and avoid STL usage."""

    mcu_root = _REPO_ROOT / "openwrt-library-arduino" / "src"
    assert mcu_root.is_dir(), f"missing MCU src root: {mcu_root}"

    cpp_files = _iter_text_files(mcu_root, ("*.h", "*.hpp", "*.c", "*.cpp"))

    # Keep this intentionally conservative: it catches the common STL types and includes.
    stl_regex = re.compile(
        r"(\bstd::|#\s*include\s*<\s*(vector|string|map|set|list|deque|array|optional|variant|tuple|memory|algorithm|functional|unordered_[^>]+)\s*>)"
    )
    hits = _find_matches(cpp_files, stl_regex)

    assert not hits, "STL usage is not allowed in openwrt-library-arduino/src:\n" + "\n".join(
        f"{path.relative_to(_REPO_ROOT)}:{line_no}: {line}" for path, line_no, line in hits
    )


def test_no_changeme_placeholder_in_shipped_defaults() -> None:
    """Never ship with the legacy placeholder serial secret."""

    forbidden = "changeme123"
    files = [
        _REPO_ROOT / "luci-app-yunbridge" / "root" / "etc" / "config" / "yunbridge",
        _REPO_ROOT / "luci-app-yunbridge" / "luasrc" / "model" / "cbi" / "yunbridge.lua",
    ]

    failures: list[str] = []
    for path in files:
        assert path.exists(), f"Missing expected defaults file: {path}"
        data = path.read_text(encoding="utf-8", errors="replace")
        if forbidden in data:
            failures.append(f"{path.relative_to(_REPO_ROOT)}: contains forbidden placeholder '{forbidden}'")

    assert not failures, "\n".join(failures)
