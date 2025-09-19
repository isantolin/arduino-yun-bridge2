#!/bin/bash
# Bridge v2 Arduino library install script

set -e

LIB_DST="$HOME/Arduino/libraries/Bridge-v2"
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

echo "Bridge v2 library installed to $LIB_DST."
