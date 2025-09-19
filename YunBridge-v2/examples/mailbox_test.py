#!/usr/bin/env python3
"""
Example: Test mailbox via YunBridge v2
Sends MAILBOX SEND and MAILBOX RECV commands to /dev/ttyATH0
"""
import serial
import time

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200

with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
    ser.write(b'MAILBOX SEND hello_mailbox\n')
    time.sleep(0.2)
    print(ser.readline().decode(errors='ignore').strip())
    ser.write(b'MAILBOX RECV\n')
    time.sleep(0.2)
    print(ser.readline().decode(errors='ignore').strip())
