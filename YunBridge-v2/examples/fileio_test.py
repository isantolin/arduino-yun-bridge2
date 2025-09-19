#!/usr/bin/env python3
"""
Example: Test file I/O via YunBridge v2
Sends WRITEFILE and READFILE commands to /dev/ttyATH0
"""
import serial
import time

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200
TEST_FILE = '/tmp/bridge_test.txt'

with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
    ser.write(f'WRITEFILE {TEST_FILE} hello_bridge\n'.encode())
    time.sleep(0.2)
    print(ser.readline().decode(errors='ignore').strip())
    ser.write(f'READFILE {TEST_FILE}\n'.encode())
    time.sleep(0.2)
    print(ser.readline().decode(errors='ignore').strip())
