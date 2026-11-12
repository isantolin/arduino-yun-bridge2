#!/usr/bin/env bash
# [SIL-2 / Rule 9 / Rule 19] Canonical delegate forwarding to unified host test & coverage pipeline.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/ci_arduino_host_tests.sh" --coverage "$@"
