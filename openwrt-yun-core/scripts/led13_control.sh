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
