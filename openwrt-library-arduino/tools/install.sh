#!/bin/bash
#
# This file is part of Arduino Yun Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# YunBridge Arduino library install script - Robust version

set -e

# Always work relative to the repository root so the script can be invoked
# from any directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Allow user to override the Arduino libraries directory
if [ -n "$1" ]; then
  LIB_DIR="$1"
  echo "[INFO] Using user-provided Arduino libraries directory: $LIB_DIR"
else
  # Determine the Arduino libraries directory automatically
  if [ -d "$HOME/Documents/Arduino/libraries" ]; then
    LIB_DIR="$HOME/Documents/Arduino/libraries"
    echo "[INFO] Found Arduino libraries at: $LIB_DIR"
  elif [ -d "$HOME/Arduino/libraries" ]; then
    LIB_DIR="$HOME/Arduino/libraries"
    echo "[INFO] Found Arduino libraries at: $LIB_DIR"
  else
    # Default to creating the standard Arduino libraries directory
    LIB_DIR="$HOME/Arduino/libraries"
    echo "[WARN] Arduino libraries directory not found. Creating it at: $LIB_DIR"
  fi
fi

mkdir -p "$LIB_DIR"

LIB_DST="$LIB_DIR/YunBridge"

ensure_packetserial_library() {
  local packetserial_dir="$LIB_DIR/PacketSerial"
  if [ -f "$packetserial_dir/PacketSerial.h" ] || [ -f "$packetserial_dir/src/PacketSerial.h" ]; then
    echo "[INFO] PacketSerial dependency already present."
    return 0
  fi

  echo "[WARN] PacketSerial library not found. Attempting to install it automatically..."
  if ! command -v unzip >/dev/null 2>&1; then
    echo "[ERROR] 'unzip' is required to install PacketSerial automatically." >&2
    exit 1
  fi

  local downloader=""
  if command -v curl >/dev/null 2>&1; then
    downloader="curl"
  elif command -v wget >/dev/null 2>&1; then
    downloader="wget"
  else
    echo "[ERROR] Install 'curl' or 'wget' to download PacketSerial automatically." >&2
    exit 1
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT

  local zip_path="$tmp_dir/PacketSerial.zip"
  local repo_url="https://codeload.github.com/bakercp/PacketSerial/zip/refs/heads/master"
  echo "[INFO] Downloading PacketSerial from $repo_url"
  if [ "$downloader" = "curl" ]; then
    curl -fsSL "$repo_url" -o "$zip_path" || {
      echo "[ERROR] Unable to download PacketSerial automatically. Please install it manually from https://github.com/bakercp/PacketSerial" >&2
      exit 1
    }
  else
    wget -qO "$zip_path" "$repo_url" || {
      echo "[ERROR] Unable to download PacketSerial automatically. Please install it manually from https://github.com/bakercp/PacketSerial" >&2
      exit 1
    }
  fi

  unzip -q "$zip_path" -d "$tmp_dir"
  local extracted_root
  extracted_root="$(find "$tmp_dir" -maxdepth 1 -type d -name 'PacketSerial-*' | head -n1)"
  if [ -z "$extracted_root" ]; then
    echo "[ERROR] Failed to extract PacketSerial archive." >&2
    exit 1
  fi

  rm -rf "$packetserial_dir"
  cp -a "$extracted_root" "$packetserial_dir"
  if [ ! -f "$packetserial_dir/src/PacketSerial.h" ]; then
    echo "[ERROR] PacketSerial installation failed; header not found at $packetserial_dir/src/PacketSerial.h" >&2
    exit 1
  fi
  echo "[OK] PacketSerial installed at $packetserial_dir"
  rm -rf "$tmp_dir"
  trap - EXIT
}

ensure_crc32_library() {
  local crc32_dir="$LIB_DIR/CRC32"
  if [ -f "$crc32_dir/src/CRC32.h" ]; then
    echo "[INFO] CRC32 dependency already present."
    return 0
  fi

  echo "[WARN] CRC32 library not found. Attempting to install it automatically..."
  if ! command -v unzip >/dev/null 2>&1; then
    echo "[ERROR] 'unzip' is required to install CRC32 automatically." >&2
    exit 1
  fi

  local downloader=""
  if command -v curl >/dev/null 2>&1; then
    downloader="curl"
  elif command -v wget >/dev/null 2>&1; then
    downloader="wget"
  else
    echo "[ERROR] Install 'curl' or 'wget' to download CRC32 automatically." >&2
    exit 1
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT

  local zip_path="$tmp_dir/CRC32.zip"
  local repo_url="https://codeload.github.com/bakercp/CRC32/zip/refs/heads/master"
  echo "[INFO] Downloading CRC32 from $repo_url"
  if [ "$downloader" = "curl" ]; then
    curl -fsSL "$repo_url" -o "$zip_path" || {
      echo "[ERROR] Unable to download CRC32 automatically. Please install it manually from https://github.com/bakercp/CRC32" >&2
      exit 1
    }
  else
    wget -qO "$zip_path" "$repo_url" || {
      echo "[ERROR] Unable to download CRC32 automatically. Please install it manually from https://github.com/bakercp/CRC32" >&2
      exit 1
    }
  fi

  unzip -q "$zip_path" -d "$tmp_dir"
  local extracted_root
  extracted_root="$(find "$tmp_dir" -maxdepth 1 -type d -name 'CRC32-*' | head -n1)"
  if [ -z "$extracted_root" ]; then
    echo "[ERROR] Failed to extract CRC32 archive." >&2
    exit 1
  fi

  rm -rf "$crc32_dir"
  cp -a "$extracted_root" "$crc32_dir"
  if [ ! -f "$crc32_dir/src/CRC32.h" ]; then
    echo "[ERROR] CRC32 installation failed; header not found at $crc32_dir/src/CRC32.h" >&2
    exit 1
  fi
  echo "[OK] CRC32 installed at $crc32_dir"
  rm -rf "$tmp_dir"
  trap - EXIT
}

ensure_crypto_library() {
  local crypto_candidates=("Crypto" "Arduino_Crypto")
  for candidate in "${crypto_candidates[@]}"; do
    if [ -f "$LIB_DIR/${candidate}/src/Crypto.h" ] || [ -f "$LIB_DIR/${candidate}/Crypto.h" ]; then
      echo "[INFO] Arduino Crypto dependency already present: $candidate"
      return 0
    fi
  done

  echo "[WARN] Arduino Crypto library not found. Attempting to install it automatically..."
  if ! command -v unzip >/dev/null 2>&1; then
    echo "[ERROR] 'unzip' is required to install the Crypto library automatically." >&2
    exit 1
  fi

  local downloader=""
  if command -v curl >/dev/null 2>&1; then
    downloader="curl"
  elif command -v wget >/dev/null 2>&1; then
    downloader="wget"
  else
    echo "[ERROR] Install 'curl' or 'wget' to download the Crypto library automatically." >&2
    exit 1
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT

  local zip_path="$tmp_dir/arduinolibs.zip"
  local repo_url="https://codeload.github.com/rweather/arduinolibs/zip/refs/heads/master"
  echo "[INFO] Downloading Crypto (rweather/arduinolibs) from $repo_url"
  if [ "$downloader" = "curl" ]; then
    curl -fsSL "$repo_url" -o "$zip_path" || {
      echo "[ERROR] Unable to download arduinolibs automatically. Please install it manually from https://github.com/rweather/arduinolibs" >&2
      exit 1
    }
  else
    wget -qO "$zip_path" "$repo_url" || {
      echo "[ERROR] Unable to download arduinolibs automatically. Please install it manually from https://github.com/rweather/arduinolibs" >&2
      exit 1
    }
  fi

  unzip -q "$zip_path" -d "$tmp_dir"
  local extracted_root
  extracted_root="$(find "$tmp_dir" -maxdepth 1 -type d -name 'arduinolibs-*' | head -n1)"
  if [ -z "$extracted_root" ]; then
    echo "[ERROR] Failed to extract arduinolibs archive." >&2
    exit 1
  fi

  local crypto_source="$extracted_root/libraries/Crypto"
  if [ ! -d "$crypto_source" ]; then
    echo "[ERROR] Crypto library not found inside arduinolibs archive." >&2
    exit 1
  fi

  local target_dir="$LIB_DIR/Crypto"
  rm -rf "$target_dir"
  cp -a "$crypto_source" "$target_dir"
  if [ ! -f "$target_dir/Crypto.h" ] && [ ! -f "$target_dir/src/Crypto.h" ]; then
    echo "[ERROR] Crypto installation failed; expected header missing in $target_dir" >&2
    exit 1
  fi
  echo "[OK] Crypto installed at $target_dir (source: rweather/arduinolibs)"
  rm -rf "$tmp_dir"
  trap - EXIT
}

ensure_crc32_library
ensure_packetserial_library
ensure_crypto_library

if [ ! -d src ]; then
	echo "ERROR: 'src' directory not found. Run this script from the 'openwrt-library-arduino' directory." >&2
	exit 1
fi

echo "[INFO] Installing YunBridge library to: $LIB_DST"
# Remove any previous installation to avoid stale files from older layouts
if [ -d "$LIB_DST" ]; then
  echo "[INFO] Clearing existing YunBridge library contents"
  rm -rf "$LIB_DST"
fi

# Recreate base layout and copy metadata
mkdir -p "$LIB_DST"
cp -a library.properties "$LIB_DST/"

# Copy source tree (retaining src/ so the IDE treats this as a modern library)
cp -a src "$LIB_DST/"

# Ship examples if present
if [ -d examples ]; then
  cp -a examples "$LIB_DST/"
fi

echo "[OK] YunBridge library installed successfully."
