#!/usr/bin/env python3
"""
YunBridge v2 Daemon
- Listens on /dev/ttyATH0 @ 115200 baud
- Implements legacy Bridge protocol and extensions
- Communicates with Arduino MCU and exposes REST/WebSocket APIs
"""
import serial
import threading
import time
import sys

SERIAL_PORT = '/dev/ttyATH0'
BAUDRATE = 115200

class BridgeDaemon:
    def __init__(self, port=SERIAL_PORT, baudrate=BAUDRATE):
        self.ser = serial.Serial(port, baudrate, timeout=1)
        self.running = True

    def handle_command(self, line):
        cmd = line.strip()
        print(f"[DEBUG] Received command: '{cmd}'")
        if cmd == 'LED13 ON':
            print("[DEBUG] Action: LED13 ON")
            self.ser.write(b'LED13:ON\n')
        elif cmd == 'LED13 OFF':
            print("[DEBUG] Action: LED13 OFF")
            self.ser.write(b'LED13:OFF\n')
        elif cmd.startswith('SET '):
            print(f"[DEBUG] Action: SET (key-value store)")
            try:
                _, key, value = cmd.split(' ', 2)
                if not hasattr(self, 'kv_store'):
                    self.kv_store = {}
                self.kv_store[key] = value
                print(f"[DEBUG] Stored: {key} = {value}")
                self.ser.write(f'OK SET {key}\n'.encode())
            except Exception as e:
                print(f"[DEBUG] SET error: {e}")
                self.ser.write(b'ERR SET\n')
        elif cmd.startswith('GET '):
            print(f"[DEBUG] Action: GET (key-value store)")
            try:
                _, key = cmd.split(' ', 1)
                value = getattr(self, 'kv_store', {}).get(key, '')
                print(f"[DEBUG] Retrieved: {key} = {value}")
                self.ser.write(f'VALUE {key} {value}\n'.encode())
            except Exception as e:
                print(f"[DEBUG] GET error: {e}")
                self.ser.write(b'ERR GET\n')
        elif cmd.startswith('RUN '):
            print(f"[DEBUG] Action: RUN (process execution)")
            import subprocess
            try:
                _, command = cmd.split(' ', 1)
                result = subprocess.getoutput(command)
                print(f"[DEBUG] RUN result: {result}")
                self.ser.write(f'RUNOUT {result}\n'.encode())
            except Exception as e:
                print(f"[DEBUG] RUN error: {e}")
                self.ser.write(b'ERR RUN\n')
        elif cmd.startswith('READFILE '):
            print(f"[DEBUG] Action: READFILE")
            try:
                _, path = cmd.split(' ', 1)
                with open(path, 'r') as f:
                    data = f.read(256)
                print(f"[DEBUG] Read from {path}: {data}")
                self.ser.write(f'FILEDATA {data}\n'.encode())
            except Exception as e:
                print(f"[DEBUG] READFILE error: {e}")
                self.ser.write(b'ERR READFILE\n')
        elif cmd.startswith('WRITEFILE '):
            print(f"[DEBUG] Action: WRITEFILE")
            try:
                _, path, data = cmd.split(' ', 2)
                with open(path, 'w') as f:
                    f.write(data)
                print(f"[DEBUG] Wrote to {path}: {data}")
                self.ser.write(b'OK WRITEFILE\n')
            except Exception as e:
                print(f"[DEBUG] WRITEFILE error: {e}")
                self.ser.write(b'ERR WRITEFILE\n')
        elif cmd.startswith('MAILBOX SEND '):
            print(f"[DEBUG] Action: MAILBOX SEND")
            if not hasattr(self, 'mailbox'):
                self.mailbox = []
            msg = cmd[len('MAILBOX SEND '):]
            self.mailbox.append(msg)
            print(f"[DEBUG] Mailbox appended: {msg}")
            self.ser.write(b'OK MAILBOX SEND\n')
        elif cmd == 'MAILBOX RECV':
            print(f"[DEBUG] Action: MAILBOX RECV")
            if hasattr(self, 'mailbox') and self.mailbox:
                msg = self.mailbox.pop(0)
                print(f"[DEBUG] Mailbox popped: {msg}")
                self.ser.write(f'MAILBOX {msg}\n'.encode())
            else:
                print(f"[DEBUG] Mailbox empty")
                self.ser.write(b'MAILBOX EMPTY\n')
        elif cmd.startswith('CONSOLE '):
            msg = cmd[len('CONSOLE '):]
            print(f'[Console] {msg}')
            print(f"[DEBUG] Action: CONSOLE")
            self.ser.write(b'OK CONSOLE\n')
        else:
            print(f"[DEBUG] Unknown command")
            self.ser.write(b'UNKNOWN COMMAND\n')

    def run(self):
        print(f"[YunBridge v2] Listening on {SERIAL_PORT} @ {BAUDRATE} baud...")
        while self.running:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors='ignore')
                    print(f"[YunBridge] Received: {line.strip()}")
                    self.handle_command(line)
                else:
                    time.sleep(0.01)
            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                print(f"[YunBridge] Error: {e}", file=sys.stderr)
                time.sleep(1)

if __name__ == '__main__':
    daemon = BridgeDaemon()
    daemon.run()
