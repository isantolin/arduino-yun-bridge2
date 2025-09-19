#!/usr/bin/env python3
"""
Example: Test LED 13 control via YunBridge v2
Sends commands to /dev/ttyATH0 to turn LED 13 on and off
"""
import serial
import time

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200

with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
    print("Turning LED 13 ON...")
    ser.write(b'LED13 ON\n')
    time.sleep(2)
    print("Turning LED 13 OFF...")
    ser.write(b'LED13 OFF\n')
    print("Done.")
