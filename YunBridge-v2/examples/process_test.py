#!/usr/bin/env python3
"""
Example: Test process execution via YunBridge v2
Sends RUN command to /dev/ttyATH0
"""
import serial
import time

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200

with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
    ser.write(b'RUN echo hello_from_yun\n')
    time.sleep(0.2)
    print(ser.readline().decode(errors='ignore').strip())
