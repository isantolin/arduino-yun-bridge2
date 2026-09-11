#!/bin/sh
# Fast OpenWrt CGI handler for McuBridge Pin Control via UBUS (SIL-2)

printf "Status: 200 OK\r\n"
printf "Content-Type: application/json\r\n"
printf "Access-Control-Allow-Origin: *\r\n"
printf "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
printf "Access-Control-Allow-Headers: Content-Type\r\n\r\n"

[ "$REQUEST_METHOD" = "OPTIONS" ] && exit 0

PIN=$(echo "$PATH_INFO" | grep -o '[0-9]\+' || echo 13)
BODY=$(head -c "${CONTENT_LENGTH:-128}")
STATE=$(echo "$BODY" | grep -i '"state"' | grep -o 'ON\|OFF\|HIGH\|LOW\|1\|0' | tr 'a-z' 'A-Z' || echo 'ON')

case "$STATE" in
    ON|HIGH|1)
        VAL=1
        NORM="ON"
        ;;
    *)
        VAL=0
        NORM="OFF"
        ;;
esac

ubus call mcubridge digital_write "{\"pin\": $PIN, \"value\": $VAL, \"ubus_rpc_session\": \"\"}" >/dev/null 2>&1

printf '{"status":"ok","data":{"pin":%d,"state":"%s"}}\n' "$PIN" "$NORM"
