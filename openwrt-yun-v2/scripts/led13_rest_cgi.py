#!/usr/bin/env python3
"""
CGI script for YunWebUI v2 REST LED 13 control
Expects POST /arduino-webui-v2/led13?state=ON|OFF
"""
import os
import sys
import serial

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200

def main():
    print('Content-Type: text/plain\n')
    query = os.environ.get('QUERY_STRING', '')
    state = 'OFF'
    if 'state=ON' in query:
        state = 'ON'
    elif 'state=OFF' in query:
        state = 'OFF'
    try:
        with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
            ser.write(f'LED13 {state}\n'.encode())
        print(f'LED 13 turned {state}')
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        print('Failed to control LED 13')

if __name__ == '__main__':
    main()
