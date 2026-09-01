from __future__ import annotations
from typing import Annotated
import typer
import asyncio
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_MANIFEST = REPO_ROOT / "hardware" / "targets.example.toml"


@dataclass
class Target:
    name: str
    host: str | None = None
    user: str | None = None
    ssh_args: list[str] = field(default_factory=list[str])
    extra_args: list[str] = field(default_factory=list[str])
    tags: set[str] = field(default_factory=set[str])
    local: bool = False
    timeout: float | None = None
    retries: int = 0
    env: dict[str, str] = field(default_factory=dict[str, str])
    notes: str | None = None


@dataclass
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


@dataclass
class ManifestDefaults:
    user: str | None = None
    timeout: float | None = None
    retries: int = 0
    ssh: list[str] | str | None = None
    tags: list[str] | str | None = None


@dataclass
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
    env: dict[str, Any] = field(default_factory=dict[str, Any])
    notes: str | None = None


@dataclass
class Manifest:
    targets: list[ManifestTarget]
    defaults: ManifestDefaults


def load_manifest(path: Path) -> list[Target]:
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
        defaults_data = data.get("defaults", {})
        defaults = ManifestDefaults(
            user=defaults_data.get("user"),
            timeout=defaults_data.get("timeout"),
            retries=defaults_data.get("retries", 0),
            ssh=defaults_data.get("ssh"),
            tags=defaults_data.get("tags"),
        )
        targets_list: list[ManifestTarget] = []
        for t in data.get("targets", []):
            targets_list.append(
                ManifestTarget(
                    name=t.get("name", ""),
                    host=t.get("host"),
                    local=t.get("local", False),
                    user=t.get("user"),
                    ssh=t.get("ssh"),
                    tags=t.get("tags"),
                    extra_args=t.get("extra_args"),
                    timeout=t.get("timeout"),
                    retries=t.get("retries"),
                    env=t.get("env", {}),
                    notes=t.get("notes"),
                )
            )
        manifest = Manifest(targets=targets_list, defaults=defaults)
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
        if not entry.name or entry.name in seen_names:
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
        env = {k: str(v) for k, v in entry.env.items()}
        parsed.append(
            Target(
                name=entry.name,
                host=entry.host if entry.host else None,
                user=user if user else None,
                ssh_args=ssh_args,
                extra_args=extra_args,
                tags=tags,
                local=entry.local,
                timeout=timeout_val,
                retries=retries,
                env=env,
                notes=entry.notes if entry.notes is not None else None,
            )
        )
    return parsed


async def run_command(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: float = 300.0
) -> tuple[int, str | None, str | None]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        def safe_decode(b: bytes) -> str:
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                return f"<hex:{b.hex()}>"

        return (proc.returncode or 0, safe_decode(stdout_bytes), safe_decode(stderr_bytes))
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except OSError as kill_err:
            sys.stderr.write(f"Failed to kill proc on timeout: {kill_err}\n")
        return (-1, None, "timeout")
    except (OSError, RuntimeError, ValueError) as e:
        return (1, None, str(e))


app = typer.Typer(help="Hardware test target harness runner.", add_completion=False)


@app.command()
def list_targets(
    manifest_path: Annotated[Path, typer.Option("--manifest", help="Path to targets.toml manifest")] = EXAMPLE_MANIFEST,
) -> None:
    """Load and validate hardware harness manifest targets."""
    targets = load_manifest(manifest_path)
    if not targets:
        print(f"No targets found in manifest: {manifest_path}")
        return
    print(f"Loaded {len(targets)} hardware target(s) from {manifest_path.name}:")
    for t in targets:
        print(f"  - {t.name} (host={t.host}, local={t.local}, tags={t.tags})")


@app.command()
def run(
    target: Annotated[str | None, typer.Option("--target", "-t", help="Target name from manifest")] = None,
    host: Annotated[str | None, typer.Option("--host", "-H", help="Target host IP or hostname")] = None,
    user: Annotated[str, typer.Option("--user", "-u", help="SSH user")] = "root",
    local: Annotated[bool, typer.Option("--local", "-l", help="Run tests locally")] = False,
    socket_path: Annotated[str, typer.Option("--socket-path", help="UNIX socket path")] = "/var/run/mcubridge.sock",
    test_name: Annotated[
        str | None, typer.Option("--test", help="Specific test name to run (e.g. led13_test.py)")
    ] = None,
    manifest_path: Annotated[Path, typer.Option("--manifest", help="Path to targets.toml manifest")] = EXAMPLE_MANIFEST,
    timeout: Annotated[float, typer.Option("--timeout", help="Timeout per test in seconds")] = 60.0,
) -> None:
    """Execute the suite of _test.py client tests locally or on remote physical hardware."""
    examples_dir = REPO_ROOT / "mcubridge-client-examples"
    available_tests = sorted([p.name for p in examples_dir.glob("*_test.py") if not p.name.startswith((".", "_"))])

    if test_name:
        clean_name = test_name if test_name.endswith(".py") else f"{test_name}.py"
        if clean_name not in available_tests:
            print(f"Error: test '{test_name}' not found. Available tests: {available_tests}")
            sys.exit(2)
        tests_to_run = [clean_name]
    else:
        tests_to_run = available_tests

    is_local = local or (host is None and target is None)
    target_host = host
    target_user = user
    ssh_args: list[str] = ["-o", "StrictHostKeyChecking=no"]

    if target and not local:
        manifest_targets = load_manifest(manifest_path)
        matched = [t for t in manifest_targets if t.name == target]
        if not matched:
            print(f"Error: target '{target}' not found in manifest {manifest_path}")
            sys.exit(2)
        tgt = matched[0]
        is_local = tgt.local
        target_host = tgt.host
        target_user = tgt.user or user
        ssh_args = tgt.ssh_args or ssh_args

    print("========================================================")
    print(" McuBridge Hardware Physical Test Suite Runner")
    print("========================================================")
    print(f"Mode: {'Local' if is_local else f'Remote SSH ({target_user}@{target_host})'}")
    print(f"Socket: {socket_path}")
    print(f"Executing {len(tests_to_run)} test script(s)...")
    print("--------------------------------------------------------")

    if not is_local and target_host:
        import subprocess

        print(f"[*] Synchronizing test scripts to {target_host}...")
        subprocess.run(
            ["ssh"] + ssh_args + [f"{target_user}@{target_host}", "mkdir -p /tmp/mcubridge-client-examples"],
            check=True,
            capture_output=True,
        )
        src_files = [str(p) for p in examples_dir.glob("*")]
        dest_remote = f"{target_user}@{target_host}:/tmp/mcubridge-client-examples/"
        subprocess.run(
            ["scp", "-O"] + ssh_args + ["-r"] + src_files + [dest_remote],
            check=True,
            capture_output=True,
        )

    results: list[tuple[str, bool, float, str | None]] = []

    async def _execute_all() -> None:
        import time

        for t_file in tests_to_run:
            sys.stdout.write(f"[*] Running {t_file}... ")
            sys.stdout.flush()
            t0 = time.time()

            if is_local:
                cmd = [
                    sys.executable,
                    str(examples_dir / t_file),
                    "--socket-path",
                    socket_path,
                ]
                env = {
                    "REPO_ROOT": str(REPO_ROOT),
                    "PYTHONPATH": f"{examples_dir}:{REPO_ROOT}",
                    "MCUBRIDGE_NON_INTERACTIVE": "1",
                }
                code, _stdout, stderr = await run_command(cmd, cwd=REPO_ROOT, env=env, timeout=timeout)
            else:
                remote_cmd = (
                    f"MCUBRIDGE_NON_INTERACTIVE=1 PYTHONPATH=/tmp/mcubridge-client-examples "
                    f"python3 /tmp/mcubridge-client-examples/{t_file} --socket-path '{socket_path}'"
                )
                cmd = ["ssh"] + ssh_args + [f"{target_user}@{target_host}", remote_cmd]
                code, _stdout, stderr = await run_command(cmd, cwd=REPO_ROOT, timeout=timeout)

            elapsed = time.time() - t0
            passed = code == 0
            status_label = "✅ [PASS]" if passed else "❌ [FAIL]"
            print(f"{status_label} ({elapsed:.2f}s)")
            results.append((t_file, passed, elapsed, stderr if not passed else None))

    asyncio.run(_execute_all())

    passed_count = sum(1 for _, p, _, _ in results if p)
    failed_count = len(results) - passed_count
    total_time = sum(el for _, _, el, _ in results)

    print("========================================================")
    print(f"SUMMARY: {passed_count} passed, {failed_count} failed in {total_time:.2f}s")
    print("========================================================")

    if failed_count > 0:
        for name, p, _, err in results:
            if not p:
                print(f"  ❌ {name}: {err or 'Unknown failure'}")
        sys.exit(1)


if __name__ == "__main__":
    app()
