#!/usr/bin/env bash
# Pigweed project environment bootstrap/activation script for Arduino MCU Bridge 2.
# Sourced by the Pigweed VSCode Extension (`launchBootstrapTerminal` / `launchTerminal`)
# and detected by `isBootstrapProject()` so the extension enables CMake / clangd
# code intelligence without requiring a Bazel workspace.

_mcubridge_project_root() {
  if [ -n "${BASH_SOURCE[0]:-}" ]; then
    (cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)
  elif [ -n "${ZSH_VERSION:-}" ]; then
    (cd "$(dirname "${(%):-%N}")" >/dev/null 2>&1 && pwd)
  else
    pwd
  fi
}

PW_PROJECT_ROOT="$(_mcubridge_project_root)"
PW_ROOT="${PW_PROJECT_ROOT}/third_party/pigweed"
export PW_PROJECT_ROOT
export PW_ROOT
export PYTHONPATH="${PW_PROJECT_ROOT}/typings:${PW_PROJECT_ROOT}/typings/stubs:${PW_PROJECT_ROOT}:${PW_PROJECT_ROOT}/mcubridge:${PW_PROJECT_ROOT}/mcubridge-client-examples:${PW_PROJECT_ROOT}/third_party/avr_pigweed_port/tools/pw_emu_avr/python${PYTHONPATH:+:${PYTHONPATH}}"
unset -f _mcubridge_project_root
