#!/bin/sh
# Wrapper that seeds PYTHONPATH before starting the YunBridge daemon.
export PYTHONPATH=/usr/libexec/yunbridge${PYTHONPATH:+:$PYTHONPATH}
exec /usr/bin/python3 -m yunbridge.daemon "$@"
