#!/bin/sh
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
