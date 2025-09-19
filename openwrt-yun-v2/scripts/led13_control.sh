#!/bin/sh
# Simple script to control LED 13 from OpenWRT
# Usage: led13_control.sh on|off

echo "LED13 $1" > /dev/ttyATH0
