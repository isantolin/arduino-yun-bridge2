#!/usr/bin/env python3
"""
Example: Test console via YunBridge v2
Sends CONSOLE command to /dev/ttyATH0
"""
import serial
import time

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200

with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
    ser.write(b'CONSOLE hello_console\n')
    time.sleep(0.2)
    print(ser.readline().decode(errors='ignore').strip())
