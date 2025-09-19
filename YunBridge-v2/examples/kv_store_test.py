#!/usr/bin/env python3
"""
Example: Test key-value store via YunBridge v2
Sends SET and GET commands to /dev/ttyATH0
"""
import serial
import time

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200

with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
    ser.write(b'SET foo bar\n')
    time.sleep(0.2)
    print(ser.readline().decode(errors='ignore').strip())
    ser.write(b'GET foo\n')
    time.sleep(0.2)
    print(ser.readline().decode(errors='ignore').strip())
