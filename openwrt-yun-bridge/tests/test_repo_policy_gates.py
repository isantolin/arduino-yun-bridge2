from __future__ import annotations

import ast
import fnmatch
import re
from os import walk
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _is_excluded_path(path: Path) -> bool:
    rel = path.relative_to(_REPO_ROOT)

    # Exclude any path that passes through these directories.
    excluded_dir_names = {
        ".tox",
        ".git",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".eggs",
        "__pycache__",
        "venv",
        "site-packages",
        "node_modules",
    }
    if any(part in excluded_dir_names for part in rel.parts):
        return True

    # Exclude whole subtrees by prefix.
    excluded_prefixes = {
        Path("build"),
        Path("dist"),
        Path("coverage"),
        Path("feeds"),
        Path("openwrt-sdk"),
        Path("test_protocol_bin"),
        Path("openwrt-library-arduino/build-coverage"),
        Path("openwrt-library-arduino/build-host"),
        Path("openwrt-library-arduino/build-host-local"),
    }
    rel_str = rel.as_posix()
    for prefix in excluded_prefixes:
        prefix_str = prefix.as_posix()
        if rel_str == prefix_str or rel_str.startswith(prefix_str + "/"):
            return True
    return False


def _iter_text_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    matched: list[Path] = []

    for dirpath_str, dirnames, filenames in walk(root):
        dirpath = Path(dirpath_str)

        # Prune excluded directories early for performance.
        if _is_excluded_path(dirpath):
            dirnames[:] = []
            continue

        # Also prune excluded subdirs.
        kept: list[str] = []
        for name in dirnames:
            p = dirpath / name
            if not _is_excluded_path(p):
                kept.append(name)
        dirnames[:] = kept

        for filename in filenames:
            if any(fnmatch.fnmatch(filename, pattern) for pattern in patterns):
                candidate = dirpath / filename
                if candidate.is_file() and not _is_excluded_path(candidate):
                    matched.append(candidate)

    return sorted(set(matched))


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


def _find_print_calls(py_files: list[Path]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            hits.append((path, exc.lineno or 1, f"SyntaxError: {exc.msg}"))
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                hits.append((path, node.lineno, "print(...)"))
    return hits


def _find_lambdas_and_nested_functions(py_files: list[Path]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function_depth = 0

        def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
            hits.append((path, node.lineno, "lambda"))
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            if self.function_depth > 0:
                hits.append((path, node.lineno, f"nested def {node.name}(...)"))
            self.function_depth += 1
            try:
                self.generic_visit(node)
            finally:
                self.function_depth -= 1

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            if self.function_depth > 0:
                hits.append((path, node.lineno, f"nested async def {node.name}(...)"))
            self.function_depth += 1
            try:
                self.generic_visit(node)
            finally:
                self.function_depth -= 1

    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            hits.append((path, exc.lineno or 1, f"SyntaxError: {exc.msg}"))
            continue

        _Visitor().visit(tree)

    return hits


def test_no_print_repo_wide() -> None:
    """Repo-wide: no print() anywhere (tools/tests/examples included).

    Rationale: production logs must remain structured (syslog/JSON), and
    helper CLIs should use explicit stdout/stderr writes.
    """

    py_files = _iter_text_files(_REPO_ROOT, ("*.py",))
    py_files = [path for path in py_files if not _is_excluded_path(path)]

    hits = _find_print_calls(py_files)

    assert not hits, "print() is not allowed repo-wide:\n" + "\n".join(
        f"{path.relative_to(_REPO_ROOT)}:{line_no}: {line}" for path, line_no, line in hits
    )


def test_no_stl_in_mcu_src_or_tests() -> None:
    """MCU code must stay C++11-friendly and avoid STL usage (src + tests)."""

    mcu_src_root = _REPO_ROOT / "openwrt-library-arduino" / "src"
    mcu_tests_root = _REPO_ROOT / "openwrt-library-arduino" / "tests"
    assert mcu_src_root.is_dir(), f"missing MCU src root: {mcu_src_root}"
    assert mcu_tests_root.is_dir(), f"missing MCU tests root: {mcu_tests_root}"

    cpp_files = _iter_text_files(mcu_src_root, ("*.h", "*.hpp", "*.c", "*.cpp"))
    cpp_files += _iter_text_files(mcu_tests_root, ("*.h", "*.hpp", "*.c", "*.cpp"))

    # Keep this intentionally conservative: it catches the common STL types and includes.
    stl_regex = re.compile(
        r"(\bstd::|#\s*include\s*<\s*(vector|string|map|set|list|"
        r"deque|array|optional|variant|tuple|memory|algorithm|functional|"
        r"unordered_[^>]+)\s*>)"
    )
    hits = _find_matches(cpp_files, stl_regex)

    message = "STL usage is not allowed in openwrt-library-arduino/src or tests:\n"
    message += "\n".join(
        f"{path.relative_to(_REPO_ROOT)}:{line_no}: {line}" for path, line_no, line in hits
    )
    assert not hits, message


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


def test_no_lambda_or_nested_functions_in_runtime_package() -> None:
    """Runtime code must avoid lambdas/closures.

    Rationale: closures (including lambdas and nested defs) can capture mutable
    state implicitly and hide aliasing. For embedded/system code, we prefer
    explicit callables (methods, top-level functions, small callable objects).
    """

    runtime_root = _REPO_ROOT / "openwrt-yun-bridge" / "yunbridge"
    assert runtime_root.is_dir(), f"missing runtime root: {runtime_root}"

    py_files = _iter_text_files(runtime_root, ("*.py",))
    hits = _find_lambdas_and_nested_functions(py_files)

    assert not hits, "Runtime package must not use lambda or nested defs:\n" + "\n".join(
        f"{path.relative_to(_REPO_ROOT)}:{line_no}: {line}" for path, line_no, line in hits
    )
