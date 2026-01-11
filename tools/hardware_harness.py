#!/usr/bin/env python3
"""Coordinate McuBridge hardware smoke tests across multiple devices."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import shlex
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence

import tomllib
import typer

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
        hint = "Copy {} to {}".format(
            EXAMPLE_MANIFEST.relative_to(REPO_ROOT),
            path.relative_to(REPO_ROOT),
        )
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
            _coerce_list(ssh_value) if ssh_value is not None else list(default_ssh)
        )
        tags = default_tags | _coerce_tags(entry.get("tags"))
        extra_value = entry.get("extra_args")
        extra_args = _coerce_list(extra_value) if extra_value is not None else []
        timeout_value = entry.get("timeout")
        if timeout_value is None:
            timeout_val = (
                float(default_timeout) if default_timeout is not None else None
            )
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
    header = (
        f"{'STATUS':8} {'TARGET':20} {'HOST':22} " f"{'ATTEMPTS':8} {'DURATION':10}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for res in results:
        host = res.target.host or "local"
        lines.append(
            f"{res.status_label:8} {res.target.name:20} {host:22} "
            f"{res.attempts:>8} {res.duration:>9.1f}s"
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
    path.write_text(json.dumps(payload, indent=2))


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


app = typer.Typer(
    add_completion=False,
    help="Run McuBridge smoke tests across multiple devices.",
)


@app.command("run")
def run_command(
    manifest: Path = typer.Option(
        DEFAULT_MANIFEST,
        "--manifest",
        "-m",
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Path to targets manifest.",
    ),
    target: list[str] | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Limit execution to the specified target name (repeatable).",
    ),
    tag: list[str] | None = typer.Option(
        None,
        "--tag",
        help="Only run targets containing the given tag (repeatable).",
    ),
    max_parallel: int = typer.Option(
        2,
        "--max-parallel",
        "-p",
        min=1,
        help="Maximum concurrent smoke runs.",
    ),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        "-T",
        min=0.0,
        help="Override per-target timeout (seconds).",
    ),
    json_path: Path | None = typer.Option(
        None,
        "--json",
        file_okay=True,
        dir_okay=False,
        writable=True,
        resolve_path=True,
        help="Write a JSON report to this path.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the commands that would run without executing them.",
    ),
    list_only: bool = typer.Option(
        False,
        "--list",
        help="List targets that match the current filters and exit.",
    ),
) -> None:
    try:
        exit_code = asyncio.run(
            main_async(
                manifest=manifest,
                targets_filter=target,
                tags_filter=tag,
                max_parallel=max_parallel,
                timeout_override=timeout,
                json_path=json_path,
                dry_run=dry_run,
                list_only=list_only,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(130)
    except FileNotFoundError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    except ValueError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    except RuntimeError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    raise typer.Exit(exit_code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
