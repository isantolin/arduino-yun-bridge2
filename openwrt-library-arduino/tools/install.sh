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
