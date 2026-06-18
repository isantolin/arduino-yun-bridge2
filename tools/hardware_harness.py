from __future__ import annotations
import asyncio
import time
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_MANIFEST = REPO_ROOT / "hardware" / "targets.example.toml"


class Target:
    name: str
    host: str | None = None
    user: str | None = None
    ssh_args: list[str] = []
    extra_args: list[str] = []
    tags: set[str] = set()
    local: bool = False
    timeout: float | None = None
    retries: int = 0
    env: dict[str, str] = {}
    notes: str | None = None


class TestResult:
    target: str
    success: bool = False
    error: str | None = None
    duration: float = 0.0
    skipped: bool = False

    def status_str(self) -> str:
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
        return [str(i) for i in cast(list[Any], value)]
    return [str(value)]


def _coerce_tags(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


class ManifestDefaults:
    user: str | None = None
    timeout: float | None = None
    retries: int = 0
    ssh: list[str] | str | None = None
    tags: list[str] | str | None = None


class ManifestTarget:
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


class Manifest:
    targets: list[ManifestTarget]
    defaults: ManifestDefaults


def load_manifest(path: Path) -> list[Target]:
    if not path.exists():
        return []

    try:
        manifest = {} # msgspec replaced
    except (OSError, Exception) as e:
        print(f"Error parsing manifest {path}: {e}")
        return []

    if not manifest.targets:
        return []

    default_ssh = _coerce_list(manifest.defaults.ssh)
    default_tags = _coerce_tags(manifest.defaults.tags)

    parsed: list[Target] = []
    seen_names: set[str] = set()
    for entry in manifest.targets:
        if not entry.name:
            continue
        if entry.name in seen_names:
            continue
        seen_names.add(entry.name)

        if not entry.local and not entry.host:
            continue

        user = entry.user if entry.user is not None else manifest.defaults.user
        ssh_args = _coerce_list(entry.ssh) if entry.ssh is not None else list(default_ssh)
        tags = default_tags | _coerce_tags(entry.tags)
        extra_args = _coerce_list(entry.extra_args) if entry.extra_args is not None else []
        timeout_val = entry.timeout if entry.timeout is not None else manifest.defaults.timeout
        retries = entry.retries if entry.retries is not None else manifest.defaults.retries
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
                notes=str(entry.notes) if entry.notes is not None else None,
            )
        )
    return parsed


async def run_command(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: float = 300.0
) -> tuple[int, str | None, str | None]:
    time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        def safe_decode(b: bytes) -> str:
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                return f"<hex:{b.hex()}>"

        return (
            proc.returncode or 0,
            safe_decode(stdout),
            safe_decode(stderr),
        )
    except TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        return (-1, None, "timeout")


def main():
    pass


if __name__ == "__main__":
    main()
