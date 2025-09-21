#!/bin/bash
# YunBridge Arduino library install script

set -e

LIB_DST="$HOME/Arduino/libraries/YunBridge"
if [ ! -d src ]; then
	echo "ERROR: src directory not found."
	exit 1
fi
if [ ! -d "$HOME/Arduino/libraries" ]; then
	echo "WARNING: $HOME/Arduino/libraries does not exist. Creating it."
	mkdir -p "$HOME/Arduino/libraries"
fi
mkdir -p "$LIB_DST"
cp -r src/* "$LIB_DST/"

echo "YunBridge library installed to $LIB_DST."
