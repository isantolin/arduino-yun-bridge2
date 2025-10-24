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

# Simple script to control LED 13 from OpenWRT via MQTT
# Usage: led13_control.sh on|off

LOGFILE="/var/log/yunbridge_script.log"
MQTT_TOPIC="br/d/13"

# Check for mosquitto_pub command
if ! command -v mosquitto_pub >/dev/null 2>&1; then
    echo "Error: mosquitto_pub command not found. Please install mosquitto-client." >&2
    echo "[$(date)] Error: mosquitto_pub command not found." >> "$LOGFILE"
    exit 3
fi

if [ -z "$1" ]; then
	echo "Usage: $0 on|off" >&2
	echo "[$(date)] Error: No argument provided" >> "$LOGFILE"
	exit 1
fi

case "$1" in
    on)
        payload="1"
        ;;
    off)
        payload="0"
        ;;
    *)
        echo "Usage: $0 on|off" >&2
        echo "[$(date)] Error: Invalid argument '$1'" >> "$LOGFILE"
        exit 2
        ;;
esac

# Publish to the MQTT topic
if ! mosquitto_pub -t "$MQTT_TOPIC" -m "$payload"; then
	echo "[$(date)] Error: Failed to publish MQTT message" >> "$LOGFILE"
	exit 4
fi
