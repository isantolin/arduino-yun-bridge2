#!/bin/sh
# Fast CGI Proxy to McuBridge REST Server (sub-millisecond latency) [SIL-2]
PIN=13
if [ -n "$PATH_INFO" ]; then
    CLEAN_PIN=$(echo "$PATH_INFO" | grep -oE '[0-9]+' | head -1)
    [ -n "$CLEAN_PIN" ] && PIN="$CLEAN_PIN"
fi

BODY=""
if [ -n "$CONTENT_LENGTH" ] && [ "$CONTENT_LENGTH" -gt 0 ] 2>/dev/null; then
    BODY=$(head -c "$CONTENT_LENGTH")
fi

STATE="ON"
case "$BODY" in
    *OFF*|*LOW*|*\"0\"*|*:0*|*false*|*FALSE*)
        STATE="OFF"
        ;;
    *)
        STATE="ON"
        ;;
esac

REQ=$(printf '{"pin": %d, "state": "%s"}' "$PIN" "$STATE")
printf "Status: 200 OK\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
wget -qO- --header="Content-Type: application/json" --post-data="$REQ" "http://127.0.0.1:8088/pin/$PIN" 2>/dev/null || printf '{"status": "error", "message": "Bridge daemon unreachable"}\n'
