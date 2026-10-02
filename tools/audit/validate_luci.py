#!/usr/bin/env python3
"""Validation script for OpenWrt LuCI JS components."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import requests.exceptions
from openwrt_luci_rpc import OpenWrtRpc
from openwrt_luci_rpc.exceptions import (
    InvalidLuciLoginError,
    InvalidLuciTokenError,
    LuciConfigError,
    LuciRpcMethodNotFoundError,
    LuciRpcUnknownError,
    PageNotFoundError,
)


def validate_luci_app() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    luci_dir = repo_root / "luci-app-mcubridge"

    if not luci_dir.exists():
        sys.stderr.write(f"Error: {luci_dir} does not exist\n")
        return 1

    errors = 0
    sys.stdout.write("[LuCI-JS] Validating JSON schema and menu definitions...\n")

    # 1. Validate JSON files
    json_files = list(luci_dir.glob("**/*.json"))
    if not json_files:
        sys.stderr.write("Error: No JSON files found in luci-app-mcubridge\n")
        return 1

    for jf in json_files:
        try:
            with Path(jf).open(encoding="utf-8") as f:
                data = json.load(f)
            sys.stdout.write(f"  ✔ {jf.relative_to(repo_root)} (valid JSON, {len(data)} entries)\n")
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"  ✖ {jf.relative_to(repo_root)}: {e}\n")
            errors += 1

    # 2. Validate JavaScript syntax
    sys.stdout.write("[LuCI-JS] Validating JavaScript views with syntax check...\n")
    js_files = list(luci_dir.glob("htdocs/luci-static/resources/view/**/*.js"))
    if not js_files:
        sys.stderr.write("Error: No JavaScript views found\n")
        return 1

    for js_file in js_files:
        res = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            sys.stdout.write(f"  ✔ {js_file.relative_to(repo_root)} (syntax OK)\n")
        else:
            sys.stderr.write(f"  ✖ {js_file.relative_to(repo_root)}:\n{res.stderr}\n")
            errors += 1

    # 3. Check route correspondence between menu and views
    menu_file = luci_dir / "root/usr/share/luci/menu.d/luci-app-mcubridge.json"
    if menu_file.exists():
        with Path(menu_file).open(encoding="utf-8") as f:
            menu_data = json.load(f)
        for route, node in menu_data.items():
            action = node.get("action", {})
            if action.get("type") == "view":
                view_path = action.get("path")
                expected_js = luci_dir / "htdocs/luci-static/resources/view" / f"{view_path}.js"
                if expected_js.exists():
                    sys.stdout.write(f"  ✔ Route '{route}' -> {expected_js.name} mapped\n")
                else:
                    sys.stderr.write(f"  ✖ Route '{route}' missing view file {expected_js}\n")
                    errors += 1

    if errors == 0:
        sys.stdout.write("[LuCI-JS] All LuCI JS components valid and ready.\n")
        return 0

    sys.stderr.write(f"[LuCI-JS] Completed with {errors} error(s).\n")
    return 1


def validate_luci_rpc_endpoint(host: str, user: str = "root", password: str = "") -> int:
    """Validate active LuCI RPC endpoints on a live OpenWrt device via openwrt-luci-rpc."""
    sys.stdout.write(f"[LuCI-RPC] Testing connection to LuCI RPC on {host}...\n")
    try:
        rpc = OpenWrtRpc(host, user, password)
        if rpc.is_logged_in():
            sys.stdout.write(f"  ✔ Connected to LuCI on {host}, authenticated successfully\n")
            return 0
        sys.stderr.write(f"  ✖ Failed to authenticate to LuCI on {host}\n")
        return 1
    except (
        InvalidLuciLoginError,
        InvalidLuciTokenError,
        LuciConfigError,
        LuciRpcMethodNotFoundError,
        LuciRpcUnknownError,
        PageNotFoundError,
        requests.exceptions.RequestException,
        OSError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"  ✖ LuCI RPC connection failed: {exc}\n")
        return 1


def main() -> int:
    exit_code = validate_luci_app()
    if "--rpc-host" in sys.argv:
        idx = sys.argv.index("--rpc-host")
        if idx + 1 < len(sys.argv):
            rpc_host = sys.argv[idx + 1]
            rpc_res = validate_luci_rpc_endpoint(rpc_host)
            if rpc_res != 0:
                return rpc_res
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
