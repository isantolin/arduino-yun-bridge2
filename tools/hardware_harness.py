#!/usr/bin/env python3
"""Coordinate YunBridge hardware smoke tests across multiple devices."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - fallback for older interpreters
    import tomli as tomllib  # type: ignore

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "tools" / "hardware_smoke_test.sh"
DEFAULT_MANIFEST = REPO_ROOT / "hardware" / "targets.toml"
EXAMPLE_MANIFEST = REPO_ROOT / "hardware" / "targets.example.toml"


@dataclass(slots=True)
class Target:
    name: str
    host: str | None
    user: str | None
    ssh_args: list[str]
    extra_args: list[str]
    tags: set[str]
    local: bool
    timeout: float | None
    retries: int
    env: dict[str, str]
    notes: str | None


@dataclass(slots=True)
class Result:
    target: Target
    success: bool
    skipped: bool
    attempts: int
    returncode: int | None
    duration: float
    stdout: str
    stderr: str
    error: str | None

    @property
    def status_label(self) -> str:
        if self.skipped:
            return "SKIPPED"
        if self.success:
            return "PASS"
        if self.error == "timeout":
            return "TIMEOUT"
        return "FAIL"


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _coerce_tags(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


def load_manifest(path: Path) -> list[Target]:
    if not path.exists():
        hint = f"Copy {EXAMPLE_MANIFEST.relative_to(REPO_ROOT)} to {path.relative_to(REPO_ROOT)}"
        raise FileNotFoundError(
            textwrap.dedent(
                f"""
                Hardware manifest {path} is missing.
                {hint} and edit it with your device list.
                """
            ).strip()
        )

    data = tomllib.loads(path.read_text())
    defaults = data.get("defaults", {}) if isinstance(data, dict) else {}

    targets_raw = data.get("targets") if isinstance(data, dict) else None
    if not targets_raw:
        raise ValueError("Manifest must define at least one [[targets]] entry")

    default_user = defaults.get("user")
    default_timeout = defaults.get("timeout")
    default_retries = int(defaults.get("retries", 0))
    default_ssh = _coerce_list(defaults.get("ssh"))
    default_tags = _coerce_tags(defaults.get("tags"))

    parsed: list[Target] = []
    seen_names: set[str] = set()
    for entry in targets_raw:
        if not isinstance(entry, dict):
            raise ValueError("Each [[targets]] entry must be a table")
        name = str(entry.get("name")) if entry.get("name") else None
        if not name:
            raise ValueError("Found target without a name")
        if name in seen_names:
            raise ValueError(f"Duplicated target name: {name}")
        seen_names.add(name)

        local = bool(entry.get("local", False))
        host = entry.get("host")
        if not local and not host:
            raise ValueError(f"Target {name} must define 'host' or set local=true")

        user = entry.get("user", default_user)
        ssh_value = entry.get("ssh")
        ssh_args = (
            _coerce_list(ssh_value)
            if ssh_value is not None
            else list(default_ssh)
        )
        tags = default_tags | _coerce_tags(entry.get("tags"))
        extra_value = entry.get("extra_args")
        extra_args = _coerce_list(extra_value) if extra_value is not None else []
        timeout_value = entry.get("timeout")
        if timeout_value is None:
            timeout_val = float(default_timeout) if default_timeout is not None else None
        else:
            timeout_val = float(timeout_value)
        retries = int(entry.get("retries", default_retries))
        env = {str(k): str(v) for k, v in entry.get("env", {}).items()}
        notes = entry.get("notes")

        parsed.append(
            Target(
                name=name,
                host=str(host) if host else None,
                user=str(user) if user else None,
                ssh_args=ssh_args,
                extra_args=extra_args,
                tags=tags,
                local=local,
                timeout=timeout_val,
                retries=retries,
                env=env,
                notes=str(notes) if notes else None,
            )
        )
    return parsed


async def _invoke_command(
    cmd: Sequence[str], *, env: dict[str, str], cwd: Path, timeout: float | None
) -> tuple[int | None, str, str, float, str | None]:
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration = time.monotonic() - start
        return (
            proc.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            duration,
            None,
        )
    except asyncio.TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        duration = time.monotonic() - start
        return (
            None,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            duration,
            "timeout",
        )


async def run_target(
    target: Target,
    *,
    dry_run: bool,
    cwd: Path,
    timeout_override: float | None,
) -> Result:
    cmd = [str(SMOKE_SCRIPT)]
    if target.local:
        cmd.append("--local")
    else:
        cmd.extend(["--host", target.host or ""])
        if target.user:
            cmd.extend(["--user", target.user])
        for arg in target.ssh_args:
            cmd.extend(["--ssh", arg])
    cmd.extend(target.extra_args)

    env = os.environ.copy()
    env.update(target.env)

    if dry_run:
        rendered = " ".join(shlex.quote(part) for part in cmd)
        return Result(
            target=target,
            success=True,
            skipped=True,
            attempts=0,
            returncode=0,
            duration=0.0,
            stdout="",
            stderr=rendered,
            error=None,
        )

    attempts = 0
    overall_stdout: list[str] = []
    overall_stderr: list[str] = []
    total_duration = 0.0
    timeout = timeout_override if timeout_override is not None else target.timeout

    while True:
        attempts += 1
        returncode, stdout, stderr, duration, error = await _invoke_command(
            cmd,
            env=env,
            cwd=cwd,
            timeout=timeout,
        )
        overall_stdout.append(stdout)
        overall_stderr.append(stderr)
        total_duration += duration
        if error == "timeout":
            return Result(
                target=target,
                success=False,
                skipped=False,
                attempts=attempts,
                returncode=None,
                duration=total_duration,
                stdout="".join(overall_stdout),
                stderr="".join(overall_stderr),
                error=error,
            )
        if returncode == 0:
            return Result(
                target=target,
                success=True,
                skipped=False,
                attempts=attempts,
                returncode=0,
                duration=total_duration,
                stdout="".join(overall_stdout),
                stderr="".join(overall_stderr),
                error=None,
            )
        if attempts > target.retries:
            return Result(
                target=target,
                success=False,
                skipped=False,
                attempts=attempts,
                returncode=returncode,
                duration=total_duration,
                stdout="".join(overall_stdout),
                stderr="".join(overall_stderr),
                error=f"exit {returncode}",
            )
        await asyncio.sleep(0.5)


def format_summary(results: Sequence[Result]) -> str:
    lines = [f"{len(results)} target(s) processed at {datetime.now().isoformat(timespec='seconds')}"]
    header = f"{'STATUS':8} {'TARGET':20} {'HOST':22} {'ATTEMPTS':8} {'DURATION':10}"
    lines.append(header)
    lines.append("-" * len(header))
    for res in results:
        host = res.target.host or "local"
        lines.append(
            f"{res.status_label:8} {res.target.name:20} {host:22} {res.attempts:>8} {res.duration:>9.1f}s"
        )
        if not res.success and not res.skipped:
            snippet = res.stderr.strip() or res.stdout.strip()
            if snippet:
                snippet = textwrap.shorten(snippet.replace("\n", " | "), width=120)
                lines.append(f"    detail: {snippet}")
    return "\n".join(lines)


def write_json(results: Sequence[Result], path: Path) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": [
            {
                "name": res.target.name,
                "host": res.target.host,
                "tags": sorted(res.target.tags),
                "status": res.status_label,
                "success": res.success,
                "attempts": res.attempts,
                "returncode": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
                "error": res.error,
            }
            for res in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run YunBridge smoke tests across multiple devices.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Path to targets manifest (default: %(default)s)",
    )
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="Limit execution to the specified target name (repeatable).",
    )
    parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        help="Only run targets that contain the given tag (repeatable).",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=2,
        help="Maximum concurrent smoke runs.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="Override per-target timeout (seconds).",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Write a JSON report to this path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands that would run without executing them.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List targets that match the current filters and exit.",
    )
    return parser


def filter_targets(
    targets: Iterable[Target],
    *,
    names: Sequence[str] | None,
    tags: Sequence[str] | None,
) -> list[Target]:
    filtered = []
    name_set = {name for name in names} if names else None
    tag_set = {tag for tag in tags} if tags else None
    for target in targets:
        if name_set and target.name not in name_set:
            continue
        if tag_set and not (target.tags & tag_set):
            continue
        filtered.append(target)
    return filtered


async def main_async(argv: Sequence[str]) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not SMOKE_SCRIPT.exists():
        parser.error(f"Smoke script missing at {SMOKE_SCRIPT}")

    try:
        targets = load_manifest(args.manifest)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    targets = filter_targets(targets, names=args.targets, tags=args.tags)
    if not targets:
        parser.error("No targets matched the provided filters.")

    if args.list:
        for target in targets:
            tag_str = ",".join(sorted(target.tags)) or "-"
            host = target.host or "local"
            print(f"{target.name:20} host={host:20} tags={tag_str}")
        return 0

    semaphore = asyncio.Semaphore(max(1, args.max_parallel))

    async def runner(target: Target) -> Result:
        async with semaphore:
            return await run_target(
                target,
                dry_run=args.dry_run,
                cwd=REPO_ROOT,
                timeout_override=args.timeout,
            )

    results = await asyncio.gather(*(runner(target) for target in targets))

    print(format_summary(results))
    if args.json:
        write_json(results, args.json)
        print(f"JSON report written to {args.json}")

    if any((not res.success) and (not res.skipped) for res in results):
        return 1
    return 0


def main() -> None:
    try:
        exit_code = asyncio.run(main_async(sys.argv[1:]))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
