#!/bin/sh
# Wrapper that seeds PYTHONPATH before starting the McuBridge daemon.
export PYTHONPATH=/usr/libexec/mcubridge${PYTHONPATH:+:$PYTHONPATH}
exec /usr/bin/python3 -O -m mcubridge.daemon "$@"
