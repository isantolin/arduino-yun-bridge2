#!/usr/bin/env python3
"""
CGI script for YunWebUI v2 REST generic pin control
Expects POST /arduino-webui-v2/pin?pin=N&state=ON|OFF
Controls any digital pin. The 'pin' parameter is required (no default).
"""
import os
import sys
import serial

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200

def parse_query(query):
    params = {}
    for part in query.split('&'):
        if '=' in part:
            k, v = part.split('=', 1)
            params[k] = v
    return params

def main():
    print('Content-Type: text/plain\n')
    query = os.environ.get('QUERY_STRING', '')
    params = parse_query(query)
    pin = params.get('pin')
    state = params.get('state', 'OFF').upper()
    if not pin or not pin.isdigit():
        print('Error: pin parameter is required and must be a number.', file=sys.stderr)
        print('Failed: pin parameter missing or invalid.')
        return
    if state not in ('ON', 'OFF'):
        state = 'OFF'
    try:
        with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
            ser.write(f'PIN{pin} {state}\n'.encode())
        print(f'Pin {pin} turned {state}')
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        print(f'Failed to control pin {pin}')

if __name__ == '__main__':
    main()
