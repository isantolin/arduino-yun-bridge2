from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, cast

import asyncssh
import typer

from tools.audit.audit_bridge_status import audit_status_dict
from tools.emulation.process_utils import terminate_pid_tree

REPO_ROOT = Path(__file__).resolve().parents[2]
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


def _coerce_env(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    res: dict[str, str] = {}
    for k, v in cast(dict[Any, Any], value).items():
        res[str(k)] = str(v)
    return res


def load_manifest(path: Path) -> list[Target]:
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        defaults_data = data.get("defaults", {})
        default_ssh = _coerce_list(defaults_data.get("ssh"))
        default_tags = _coerce_tags(defaults_data.get("tags"))
        default_user = defaults_data.get("user")
        default_timeout = defaults_data.get("timeout")
        default_retries = defaults_data.get("retries", 0)

        targets_data = data.get("targets", [])
        if not targets_data:
            return []

        parsed: list[Target] = []
        seen_names: set[str] = set()
        for entry in targets_data:
            name = entry.get("name", "")
            if not name or name in seen_names:
                continue
            local = bool(entry.get("local", False))
            host = entry.get("host")
            if not local and not host:
                continue
            seen_names.add(name)

            user = entry.get("user") if "user" in entry else default_user
            ssh_args = _coerce_list(entry.get("ssh")) if "ssh" in entry else list(default_ssh)
            tags = default_tags | _coerce_tags(entry.get("tags"))
            extra_args = _coerce_list(entry.get("extra_args")) if "extra_args" in entry else []
            timeout_val = entry.get("timeout") if "timeout" in entry else default_timeout
            retries = entry.get("retries") if "retries" in entry else default_retries
            env = _coerce_env(entry.get("env"))

            parsed.append(
                Target(
                    name=name,
                    host=host or None,
                    user=user or None,
                    ssh_args=ssh_args,
                    extra_args=extra_args,
                    tags=tags,
                    local=local,
                    timeout=timeout_val,
                    retries=retries,
                    env=env,
                    notes=entry.get("notes"),
                )
            )
        return parsed
    except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError) as e:
        print(f"Error parsing manifest {path}: {e}")
        return []


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
        async with asyncio.timeout(timeout):
            stdout_bytes, stderr_bytes = await proc.communicate()

        def safe_decode(b: bytes) -> str:
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                return f"<hex:{b.hex()}>"

        return (proc.returncode or 0, safe_decode(stdout_bytes), safe_decode(stderr_bytes))
    except TimeoutError:
        terminate_pid_tree(proc.pid, timeout=2.0)
        try:
            proc.kill()
        except OSError as kill_err:
            sys.stderr.write(f"Failed to kill proc on timeout: {kill_err}\n")
        return (-1, None, "timeout")
    except (OSError, RuntimeError, ValueError) as e:
        return (1, None, str(e))


async def run_ssh_command(
    host: str,
    user: str,
    cmd: str,
    timeout: float = 300.0,
) -> tuple[int, str, str]:
    """Execute command on remote host directly via asyncssh without fallbacks."""
    async with asyncssh.connect(host, username=user, known_hosts=None) as conn:
        res = await asyncio.wait_for(conn.run(cmd), timeout=timeout)
        return res.exit_status or 0, str(res.stdout or ""), str(res.stderr or "")


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
    gateway_host: Annotated[str, typer.Option("--gateway-host", help="Gateway host")] = "127.0.0.1",
    gateway_port: Annotated[int, typer.Option("--gateway-port", help="Gateway port")] = 8443,
    device_id: Annotated[str, typer.Option("--device-id", help="Explicit target device ID")] = "yun-01",
    test_name: Annotated[
        str | None, typer.Option("--test", help="Specific test name to run (e.g. led13_test.py)")
    ] = None,
    manifest_path: Annotated[Path, typer.Option("--manifest", help="Path to targets.toml manifest")] = EXAMPLE_MANIFEST,
    timeout: Annotated[float, typer.Option("--timeout", help="Timeout per test in seconds")] = 60.0,
) -> None:
    """Execute the suite of _test.py client tests locally or on remote physical hardware."""
    examples_parent = REPO_ROOT / "mcubridge-client-examples"
    examples_dir = examples_parent / "examples"
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
    print(f"Gateway: {gateway_host}:{gateway_port} (Device: {device_id})")
    print(f"Executing {len(tests_to_run)} test script(s)...")
    print("--------------------------------------------------------")

    if not is_local and target_host:
        from tools.emulation.sync_to_vm import tar_push

        print(f"[*] Synchronizing test scripts to {target_host}...")
        tar_push(
            src_dir=examples_parent,
            remote_dest="/tmp/mcubridge-client-examples",
            host=target_host,
            user=target_user,
            excludes=["__pycache__", "*.pyc", "*.pyo"],
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
                    "--host",
                    gateway_host,
                    "--port",
                    str(gateway_port),
                    "--device-id",
                    device_id,
                ]
                env = {
                    "REPO_ROOT": str(REPO_ROOT),
                    "PYTHONPATH": f"{examples_parent}:{REPO_ROOT / 'mcubridge'}:{REPO_ROOT}",
                    "MCUBRIDGE_NON_INTERACTIVE": "1",
                    "MCUBRIDGE_GATEWAY_HOST": gateway_host,
                    "MCUBRIDGE_GATEWAY_PORT": str(gateway_port),
                    "MCUBRIDGE_DEVICE_ID": device_id,
                }
                code, _stdout, stderr = await run_command(cmd, cwd=REPO_ROOT, env=env, timeout=timeout)
            else:
                assert target_host is not None
                target_u = target_user or "root"
                remote_cmd = (
                    f"MCUBRIDGE_NON_INTERACTIVE=1 PYTHONPATH=/tmp/mcubridge-client-examples "
                    f"python3 /tmp/mcubridge-client-examples/examples/{t_file} "
                    f"--host '{gateway_host}' --port {gateway_port} --device-id '{device_id}'"
                )
                code, _stdout, stderr = await run_ssh_command(
                    host=target_host,
                    user=target_u,
                    cmd=remote_cmd,
                    timeout=timeout,
                )

            elapsed = time.time() - t0
            passed = code == 0
            status_label = "✅ [PASS]" if passed else "❌ [FAIL]"
            print(f"{status_label} ({elapsed:.2f}s)")
            if _stdout and _stdout.strip():
                for line in _stdout.strip().splitlines():
                    print(f"      {line}")
            if stderr and stderr.strip():
                for line in stderr.strip().splitlines():
                    print(f"      [stderr] {line}")
            results.append((t_file, passed, elapsed, stderr if not passed else None))

    asyncio.run(_execute_all())

    passed_count = sum(1 for _, p, _, _ in results if p)
    failed_count = len(results) - passed_count
    total_time = sum(el for _, _, el, _ in results)

    print("========================================================")
    print(f"SUMMARY: {passed_count} passed, {failed_count} failed in {total_time:.2f}s")
    print("========================================================")

    # [SIL-2] Status Snapshot Post-Run Integrity Gate
    status_errors: list[str] = []
    if is_local:
        status_file = Path("/tmp/mcubridge_status.json")
        if status_file.exists():
            try:
                status_errors = audit_status_dict(json.loads(status_file.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
                sys.stderr.write(f"[WARN] Failed to parse /tmp/mcubridge_status.json: {e}\n")
                status_errors = [f"Failed to parse /tmp/mcubridge_status.json: {e}"]
    else:
        assert target_host is not None
        target_u = target_user or "root"
        try:
            remote_read_cmd = (
                "ubus call mcubridge status 2>/dev/null || cat /tmp/mcubridge_status.json 2>/dev/null || echo ''"
            )
            _, status_out, _ = asyncio.run(
                run_ssh_command(host=target_host, user=target_u, cmd=remote_read_cmd, timeout=10.0)
            )
            if status_out.strip():
                status_errors = audit_status_dict(json.loads(status_out.strip()))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            sys.stderr.write(f"[WARN] Failed to retrieve remote status via UBUS/file: {e}\n")
            status_errors = [f"Failed to retrieve remote status via UBUS/file: {e}"]

    if status_errors:
        print("❌ [STATUS AUDIT FAIL] Post-test status health check failed:")
        for err in status_errors:
            print(f"   • {err}")
        sys.exit(1)

    if failed_count > 0:
        for name, p, _, err in results:
            if not p:
                print(f"  ❌ {name}: {err or 'Unknown failure'}")
        sys.exit(1)


def _load_rotate_module() -> Any:
    import importlib.util

    script_path = REPO_ROOT / "mcubridge" / "scripts" / "mcubridge_rotate_credentials.py"
    spec = importlib.util.spec_from_file_location("mcubridge_rotate_credentials", str(script_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@app.command()
def rotate(
    host: Annotated[str | None, typer.Option("--host", help="Target McuBridge host (IP or DNS)")] = None,
    user: Annotated[str, typer.Option("--user", help="SSH username")] = "root",
    local: Annotated[
        Path | None,
        typer.Option("--local", help="Rotate credentials inside local UCI config directory without SSH"),
    ] = None,
    emit_sketch_snippet: Annotated[
        Path | None,
        typer.Option("--emit-sketch-snippet", help="Write BRIDGE_SERIAL_SHARED_SECRET snippet to file"),
    ] = None,
    length: Annotated[int, typer.Option("--length", help="Length of the random secret in bytes")] = 32,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force rotation without confirmation")] = False,
    no_restart: Annotated[bool, typer.Option("--no-restart", help="Skip service restart")] = False,
    ssh_args: Annotated[list[str] | None, typer.Option("--ssh", help="Extra SSH options")] = None,
) -> None:
    """Rotate MCU Bridge shared credentials on remote hardware or local rootfs."""
    import re

    secret: str | None = None
    output: str = ""

    if local:
        if not local.is_dir():
            print(f"Error: --local directory {local} does not exist", file=sys.stderr)
            sys.exit(1)
        os.environ["UCI_CONFIG_DIR"] = str(local)
        mod = _load_rotate_module()
        secret, _ = mod.generate_and_apply_credentials(length=length, no_restart=no_restart)
        output = f"SERIAL_SECRET={secret}\n"
    elif host:
        remote_cmd = f"/usr/bin/mcubridge-rotate-credentials --length {length}"
        if force:
            remote_cmd += " --force"
        if no_restart:
            remote_cmd += " --no-restart"

        rot_code, rot_out, rot_err = asyncio.run(run_ssh_command(host=host, user=user, cmd=remote_cmd, timeout=30.0))
        if rot_code != 0:
            print(f"Error running remote rotation on {host}: {rot_err}", file=sys.stderr)
            sys.exit(rot_code)
        output = rot_out
    else:
        print("Error: Either --host or --local is required.", file=sys.stderr)
        sys.exit(1)

    match = re.search(r"SERIAL_SECRET=([a-fA-F0-9]+)", output)
    if match:
        secret = match.group(1)

    if not secret:
        print(f"Warning: Could not extract SERIAL_SECRET from output:\n{output}", file=sys.stderr)
        return

    snippet = (
        f"#pragma once\n"
        f"// Include this header before <Bridge.h> in your sketch sources.\n"
        f'#define BRIDGE_SERIAL_SHARED_SECRET "{secret}"\n'
        f"#define BRIDGE_SERIAL_SHARED_SECRET_LEN (sizeof(BRIDGE_SERIAL_SHARED_SECRET) - 1)\n"
    )

    if emit_sketch_snippet:
        emit_sketch_snippet.parent.mkdir(parents=True, exist_ok=True)
        emit_sketch_snippet.write_text(snippet, encoding="utf-8")
        print(f"[INFO] Wrote sketch snippet to {emit_sketch_snippet}")

    print("\n" + snippet)


if __name__ == "__main__":
    app()
