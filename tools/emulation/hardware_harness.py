from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import asyncssh
import typer

from tools.audit.audit_bridge_status import audit_status_dict
from tools.emulation.process_utils import terminate_pid_tree

REPO_ROOT = Path(__file__).resolve().parents[2]
LED_BUILTIN_TESTS = frozenset({"led13_test.py", "all_features_test.py"})


def requires_led_builtin_pin(test_name: str) -> bool:
    return test_name in LED_BUILTIN_TESTS


def led_pin_arguments(test_name: str, led_builtin_pin: int | None) -> list[str]:
    if not requires_led_builtin_pin(test_name):
        return []
    if led_builtin_pin is None:
        raise typer.BadParameter("--led-builtin-pin is required when running led13_test.py or all_features_test.py")
    return ["--pin", str(led_builtin_pin)]


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


def _resolve_tests_to_run(examples_dir: Path, test_name: str | None) -> list[str]:
    available_tests = sorted([p.name for p in examples_dir.glob("*_test.py") if not p.name.startswith((".", "_"))])
    if not test_name:
        return available_tests
    clean_name = test_name if test_name.endswith(".py") else f"{test_name}.py"
    if clean_name not in available_tests:
        print(f"Error: test '{test_name}' not found. Available tests: {available_tests}")
        sys.exit(2)
    return [clean_name]


def _sync_test_scripts(examples_parent: Path, target_host: str, target_user: str) -> None:
    from tools.emulation.sync_to_vm import tar_push

    print(f"[*] Synchronizing test scripts to {target_host}...")
    tar_push(
        src_dir=examples_parent,
        remote_dest="/tmp/mcubridge-client-examples",
        host=target_host,
        user=target_user,
        excludes=["__pycache__", "*.pyc", "*.pyo"],
    )


async def _execute_single_test(
    t_file: str,
    *,
    is_local: bool,
    target_host: str | None,
    target_user: str,
    examples_parent: Path,
    examples_dir: Path,
    gateway_host: str,
    gateway_port: int,
    device_id: str,
    pin_args: list[str],
    timeout: float,
) -> tuple[str, bool, float, str | None]:
    import time

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
        cmd.extend(pin_args)
        env = {
            "REPO_ROOT": str(REPO_ROOT),
            "PYTHONPATH": f"{examples_parent}:{REPO_ROOT / 'mcubridge'}:{REPO_ROOT}",
            "MCUBRIDGE_NON_INTERACTIVE": "1",
            "MCUBRIDGE_GATEWAY_HOST": gateway_host,
            "MCUBRIDGE_GATEWAY_PORT": str(gateway_port),
            "MCUBRIDGE_DEVICE_ID": device_id,
        }
        code, stdout, stderr = await run_command(cmd, cwd=REPO_ROOT, env=env, timeout=timeout)
    else:
        assert target_host is not None
        target_u = target_user or "root"
        remote_cmd = (
            f"MCUBRIDGE_NON_INTERACTIVE=1 PYTHONPATH=/tmp/mcubridge-client-examples "
            f"python3 /tmp/mcubridge-client-examples/examples/{t_file} "
            f"--host '{gateway_host}' --port {gateway_port} --device-id '{device_id}'"
        )
        remote_cmd += "".join(f" {arg}" for arg in pin_args)
        code, stdout, stderr = await run_ssh_command(
            host=target_host,
            user=target_u,
            cmd=remote_cmd,
            timeout=timeout,
        )

    elapsed = time.time() - t0
    passed = code == 0
    status_label = "✅ [PASS]" if passed else "❌ [FAIL]"
    print(f"{status_label} ({elapsed:.2f}s)")
    if stdout and stdout.strip():
        for line in stdout.strip().splitlines():
            print(f"      {line}")
    if stderr and stderr.strip():
        for line in stderr.strip().splitlines():
            print(f"      [stderr] {line}")
    return (t_file, passed, elapsed, stderr if not passed else None)


def _audit_post_run_status(is_local: bool, target_host: str | None, target_user: str) -> list[str]:
    if is_local:
        status_file = Path("/tmp/mcubridge_status.json")
        if not status_file.exists():
            return []
        try:
            return audit_status_dict(json.loads(status_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            sys.stderr.write(f"[WARN] Failed to parse /tmp/mcubridge_status.json: {e}\n")
            return [f"Failed to parse /tmp/mcubridge_status.json: {e}"]

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
            return audit_status_dict(json.loads(status_out.strip()))
        return []
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
        sys.stderr.write(f"[WARN] Failed to retrieve remote status via UBUS/file: {e}\n")
        return [f"Failed to retrieve remote status via UBUS/file: {e}"]


def _report_results_and_exit(
    results: list[tuple[str, bool, float, str | None]],
    status_errors: list[str],
) -> None:
    passed_count = sum(1 for _, p, _, _ in results if p)
    failed_count = len(results) - passed_count
    total_time = sum(el for _, _, el, _ in results)

    print("========================================================")
    print(f"SUMMARY: {passed_count} passed, {failed_count} failed in {total_time:.2f}s")
    print("========================================================")

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


@app.command()
def run(
    host: Annotated[str | None, typer.Option("--host", "-H", help="Target host IP or hostname")] = None,
    user: Annotated[str, typer.Option("--user", "-u", help="SSH user")] = "root",
    local: Annotated[bool, typer.Option("--local", "-l", help="Run tests locally")] = False,
    gateway_host: Annotated[str, typer.Option("--gateway-host", help="Gateway host")] = "127.0.0.1",
    gateway_port: Annotated[int, typer.Option("--gateway-port", help="Gateway port")] = 8443,
    device_id: Annotated[str, typer.Option("--device-id", help="Explicit target device ID")] = "yun-01",
    led_builtin_pin: Annotated[
        int | None,
        typer.Option("--led-builtin-pin", help="LED_BUILTIN from the target Arduino core"),
    ] = None,
    test_name: Annotated[
        str | None, typer.Option("--test", help="Specific test name to run (e.g. led13_test.py)")
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout", help="Timeout per test in seconds")] = 60.0,
) -> None:
    """Execute the suite of _test.py client tests locally or on remote physical hardware."""
    examples_parent = REPO_ROOT / "mcubridge-client-examples"
    examples_dir = examples_parent / "examples"
    tests_to_run = _resolve_tests_to_run(examples_dir, test_name)
    pin_arguments = {name: led_pin_arguments(name, led_builtin_pin) for name in tests_to_run}

    is_local, target_host, target_user = local or host is None, host, user

    print("========================================================")
    print(" McuBridge Hardware Physical Test Suite Runner")
    print("========================================================")
    print(f"Mode: {'Local' if is_local else f'Remote SSH ({target_user}@{target_host})'}")
    print(f"Gateway: {gateway_host}:{gateway_port} (Device: {device_id})")
    print(f"Executing {len(tests_to_run)} test script(s)...")
    print("--------------------------------------------------------")

    if not is_local and target_host:
        _sync_test_scripts(examples_parent, target_host, target_user)

    async def _execute_all() -> list[tuple[str, bool, float, str | None]]:
        res: list[tuple[str, bool, float, str | None]] = []
        for t_file in tests_to_run:
            test_res = await _execute_single_test(
                t_file,
                is_local=is_local,
                target_host=target_host,
                target_user=target_user,
                examples_parent=examples_parent,
                examples_dir=examples_dir,
                gateway_host=gateway_host,
                gateway_port=gateway_port,
                device_id=device_id,
                pin_args=pin_arguments[t_file],
                timeout=timeout,
            )
            res.append(test_res)
        return res

    results = asyncio.run(_execute_all())
    status_errors = _audit_post_run_status(is_local, target_host, target_user)
    _report_results_and_exit(results, status_errors)


def _load_rotate_module() -> Any:
    import importlib.util

    script_path = REPO_ROOT / "mcubridge" / "scripts" / "mcubridge_rotate_credentials.py"
    spec = importlib.util.spec_from_file_location("mcubridge_rotate_credentials", str(script_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rotate(
    host: str | None = None,
    user: str = "root",
    local: Path | None = None,
    emit_sketch_snippet: Path | None = None,
    length: int = 32,
    force: bool = False,
    no_restart: bool = False,
    ssh_args: list[str] | None = None,
    rotate_loader: Callable[[], Any] | None = None,
) -> None:
    """Rotate MCU Bridge shared credentials on remote hardware or local rootfs."""
    secret: str | None = None
    output: str = ""

    if local:
        if not local.is_dir():
            print(f"Error: --local directory {local} does not exist", file=sys.stderr)
            sys.exit(1)
        os.environ["UCI_CONFIG_DIR"] = str(local)
        mod = (rotate_loader or _load_rotate_module)()
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

    for line in output.splitlines():
        key, sep, val = line.partition("=")
        if sep and key.strip() == "SERIAL_SECRET":
            chars: list[str] = []
            for c in val.strip():
                if c in "0123456789abcdefABCDEF":
                    chars.append(c)
                else:
                    break
            if chars:
                secret = "".join(chars)
                break

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


@app.command("rotate")
def rotate_cmd(
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
    rotate(
        host=host,
        user=user,
        local=local,
        emit_sketch_snippet=emit_sketch_snippet,
        length=length,
        force=force,
        no_restart=no_restart,
        ssh_args=ssh_args,
    )


if __name__ == "__main__":
    app()
