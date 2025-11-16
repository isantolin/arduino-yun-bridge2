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

PIN_ARG=${2:-}
PIN_ENV=${YUNBRIDGE_LED_PIN:-}
PIN_DEFAULT=13
PIN="${PIN_ARG:-${PIN_ENV:-$PIN_DEFAULT}}"

if ! echo "$PIN" | grep -Eq '^[0-9]+$'; then
    echo "Error: pin must be numeric." >&2
    echo "[$(date)] Error: invalid pin '$PIN'" >> "$LOGFILE"
    exit 5
fi

TOPIC_PREFIX=$(uci -q get yunbridge.general.mqtt_topic 2>/dev/null || printf '%s' "br")
MQTT_TOPIC=${YUNBRIDGE_TOPIC:-${TOPIC_PREFIX}/d/${PIN}}

MQTT_HOST=${YUNBRIDGE_MQTT_HOST:-$(uci -q get yunbridge.general.mqtt_host 2>/dev/null || printf '%s' "127.0.0.1")}
MQTT_PORT=${YUNBRIDGE_MQTT_PORT:-$(uci -q get yunbridge.general.mqtt_port 2>/dev/null || printf '%s' "1883")}
MQTT_USER=${YUNBRIDGE_MQTT_USER:-$(uci -q get yunbridge.general.mqtt_user 2>/dev/null || printf '%s' "")}
MQTT_PASS=${YUNBRIDGE_MQTT_PASS:-$(uci -q get yunbridge.general.mqtt_pass 2>/dev/null || printf '%s' "")}

# Check for mosquitto_pub command
if ! command -v mosquitto_pub >/dev/null 2>&1; then
    echo "Error: mosquitto_pub command not found. Please install mosquitto-client." >&2
    echo "[$(date)] Error: mosquitto_pub command not found." >> "$LOGFILE"
    exit 3
fi

if [ -z "$1" ]; then
    echo "Usage: $0 on|off [pin]" >&2
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

# Build mosquitto command using positional parameters for safe quoting
set -- mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t "$MQTT_TOPIC" -m "$payload"
if [ -n "$MQTT_USER" ]; then
    set -- "$@" -u "$MQTT_USER"
fi
if [ -n "$MQTT_PASS" ]; then
    set -- "$@" -P "$MQTT_PASS"
fi

if ! "$@"; then
	echo "[$(date)] Error: Failed to publish MQTT message" >> "$LOGFILE"
	exit 4
fi
