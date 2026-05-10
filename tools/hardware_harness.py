#!/usr/bin/env python3
"""Coordinate McuBridge hardware smoke tests across multiple devices."""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
import textwrap
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import msgspec

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
        items = cast(list[Any], value)
        return [str(item) for item in items]
    return [str(value)]


def _coerce_tags(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


def load_manifest(path: Path) -> list[Target]:
    if not path.exists():
        hint = "Copy {} to {}".format(
            EXAMPLE_MANIFEST.relative_to(REPO_ROOT),
            path.relative_to(REPO_ROOT),
        )
        raise FileNotFoundError(textwrap.dedent(f"""
                Hardware manifest {path} is missing.
                {hint} and edit it with your device list.
                """).strip())


class ManifestDefaults(msgspec.Struct):
    user: str | None = None
    timeout: float | None = None
    retries: int = 0
    ssh: list[str] | str | None = None
    tags: list[str] | str | None = None


class ManifestTarget(msgspec.Struct):
    name: str
    host: str | None = None
    local: bool = False
    user: str | None = None
    ssh: list[str] | str | None = None
    tags: list[str] | str | None = None
    extra_args: list[str] | str | None = None
    timeout: float | None = None
    retries: int | None = None
    env: dict[str, Any] = {}
    notes: str | None = None


class Manifest(msgspec.Struct):
    targets: list[ManifestTarget]
    defaults: ManifestDefaults = msgspec.field(default_factory=ManifestDefaults)


def parse_manifest(path: Path) -> list[Target]:
    if not path.is_file():
        raise ValueError(textwrap.dedent(f"""
                Manifest not found at {path}
                To create one, copy hardware/targets.example.toml to {path}
                and edit it with your device list.
                """).strip())

    manifest = msgspec.toml.decode(path.read_bytes(), type=Manifest)

    if not manifest.targets:
        raise ValueError("Manifest must define at least one [[targets]] entry")

    default_ssh = _coerce_list(manifest.defaults.ssh)
    default_tags = _coerce_tags(manifest.defaults.tags)

    parsed: list[Target] = []
    seen_names: set[str] = set()
    for entry in manifest.targets:
        if not entry.name:
            raise ValueError("Found target without a name")
        if entry.name in seen_names:
            raise ValueError(f"Duplicated target name: {entry.name}")
        seen_names.add(entry.name)

        if not entry.local and not entry.host:
            raise ValueError(
                f"Target {entry.name} must define 'host' or set local=true"
            )

        user = entry.user if entry.user is not None else manifest.defaults.user
        ssh_args = (
            _coerce_list(entry.ssh) if entry.ssh is not None else list(default_ssh)
        )
        tags = default_tags | _coerce_tags(entry.tags)
        extra_args = (
            _coerce_list(entry.extra_args) if entry.extra_args is not None else []
        )
        timeout_val = (
            entry.timeout if entry.timeout is not None else manifest.defaults.timeout
        )
        retries = (
            entry.retries if entry.retries is not None else manifest.defaults.retries
        )
        env = {str(k): str(v) for k, v in entry.env.items()}

        parsed.append(
            Target(
                name=entry.name,
                host=str(entry.host) if entry.host else None,
                user=str(user) if user else None,
                ssh_args=ssh_args,
                extra_args=extra_args,
                tags=tags,
                local=entry.local,
                timeout=timeout_val,
                retries=retries,
                env=env,
                notes=str(entry.notes) if entry.notes else None,
            )
        )
    return parsed


async def _invoke_command(
    cmd: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float | None,
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
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
        duration = time.monotonic() - start
        return (
            proc.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            duration,
            None,
        )
    except TimeoutError:
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
        rendered = shlex.join(cmd)
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
    timestamp = datetime.now().isoformat(timespec="seconds")
    lines = [f"{len(results)} target(s) processed at {timestamp}"]
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
                snippet = textwrap.shorten(
                    snippet.replace("\n", " | "),
                    width=120,
                )
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
    path.write_bytes(msgspec.json.encode(payload))


def filter_targets(
    targets: Iterable[Target],
    *,
    names: Sequence[str] | None,
    tags: Sequence[str] | None,
) -> list[Target]:
    filtered: list[Target] = []
    name_set = set(names) if names else None
    tag_set = set(tags) if tags else None
    for target in targets:
        if name_set and target.name not in name_set:
            continue
        if tag_set and not (target.tags & tag_set):
            continue
        filtered.append(target)
    return filtered


async def main_async(
    *,
    manifest: Path,
    targets_filter: Sequence[str] | None,
    tags_filter: Sequence[str] | None,
    max_parallel: int,
    timeout_override: float | None,
    json_path: Path | None,
    dry_run: bool,
    list_only: bool,
) -> int:
    if not SMOKE_SCRIPT.exists():
        raise FileNotFoundError(f"Smoke script missing at {SMOKE_SCRIPT}")

    targets = load_manifest(manifest)

    targets = filter_targets(
        targets,
        names=targets_filter,
        tags=tags_filter,
    )
    if not targets:
        raise RuntimeError("No targets matched the provided filters.")

    if list_only:
        for target in targets:
            tag_str = ",".join(sorted(target.tags)) or "-"
            host = target.host or "local"
            sys.stdout.write(f"{target.name:20} host={host:20} tags={tag_str}\n")
        return 0

    semaphore = asyncio.Semaphore(max(1, max_parallel))

    async def runner(target: Target) -> Result:
        async with semaphore:
            return await run_target(
                target,
                dry_run=dry_run,
                cwd=REPO_ROOT,
                timeout_override=timeout_override,
            )

    results = await asyncio.gather(*(runner(target) for target in targets))

    sys.stdout.write(format_summary(results) + "\n")
    if json_path:
        write_json(results, json_path)
        sys.stdout.write(f"JSON report written to {json_path}\n")

    if any((not res.success) and (not res.skipped) for res in results):
        return 1
    return 0


def run_command(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run McuBridge smoke tests across multiple devices."
    )
    parser.add_argument(
        "--manifest",
        "-m",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to targets manifest.",
    )
    parser.add_argument(
        "--target",
        "-t",
        action="append",
        dest="target",
        default=None,
        help="Limit execution to the specified target name (repeatable).",
    )
    parser.add_argument(
        "--tag",
        action="append",
        dest="tag",
        default=None,
        help="Only run targets containing the given tag (repeatable).",
    )
    parser.add_argument(
        "--max-parallel",
        "-p",
        type=int,
        default=2,
        help="Maximum concurrent smoke runs.",
    )
    parser.add_argument(
        "--timeout",
        "-T",
        type=float,
        default=None,
        help="Override per-target timeout (seconds).",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        default=None,
        help="Write a JSON report to this path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the commands that would run without executing them.",
    )
    parser.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        default=False,
        help="List targets that match the current filters and exit.",
    )
    args = parser.parse_args(argv)
    try:
        exit_code = asyncio.run(
            main_async(
                manifest=args.manifest,
                targets_filter=args.target,
                tags_filter=args.tag,
                max_parallel=args.max_parallel,
                timeout_override=args.timeout,
                json_path=args.json_path,
                dry_run=args.dry_run,
                list_only=args.list_only,
            )
        )
    except KeyboardInterrupt:
        raise SystemExit(130)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(exit_code)


def main() -> None:
    run_command()


if __name__ == "__main__":
    main()
