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
echo "LED13 $1" > /dev/ttyATH0

#!/bin/sh
# Simple script to control LED 13 from OpenWRT
# Usage: led13_control.sh on|off

LOGFILE="/tmp/yunbridge_script.log"

if [ -z "$1" ]; then
	echo "Usage: $0 on|off" >&2
	echo "[$(date)] Error: No argument provided" >> "$LOGFILE"
	exit 1
fi

if [ "$1" != "on" ] && [ "$1" != "off" ]; then
	echo "Usage: $0 on|off" >&2
	echo "[$(date)] Error: Invalid argument '$1'" >> "$LOGFILE"
	exit 2
fi

if ! [ -w /dev/ttyATH0 ]; then
	echo "Error: /dev/ttyATH0 not writable or not present" >&2
	echo "[$(date)] Error: /dev/ttyATH0 not writable or not present" >> "$LOGFILE"
	exit 3
fi

echo "LED13 $1" > /dev/ttyATH0 2>> "$LOGFILE"
if [ $? -ne 0 ]; then
	echo "[$(date)] Error: Failed to write to /dev/ttyATH0" >> "$LOGFILE"
	exit 4
fi
