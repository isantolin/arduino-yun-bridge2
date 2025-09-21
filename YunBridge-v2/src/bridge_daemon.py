import os
def debug_log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    try:
        with open('/tmp/yunbridge_debug.log', 'a') as f:
            f.write(line)
    except Exception:
        pass
    if DEBUG:
        print(line, end='')
#!/usr/bin/env python3

import serial
import time
import paho.mqtt.client as mqtt
try:
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    CallbackAPIVersion = None
import threading
import sys
try:
    import uci
except ImportError:
    uci = None



# Defaults
DEFAULTS = {
    'mqtt_host': '127.0.0.1',
    'mqtt_port': 1883,
    'mqtt_topic': 'yun/pin',
    'serial_port': '/dev/ttyATH0',
    'serial_baud': 115200,
    'debug': 0
}

def get_uci_config():
    cfg = DEFAULTS.copy()
    if uci is not None:
        try:
            c = uci.UCI()
            section = c.get('yunbridge', 'main')
            if section:
                for k in DEFAULTS:
                    v = section.get(k)
                    if v is not None:
                        if k == 'mqtt_port' or k == 'serial_baud' or k == 'debug':
                            try:
                                v = int(v)
                            except Exception:
                                v = DEFAULTS[k]
                        cfg[k] = v
        except Exception as e:
            print(f'[WARN] Error reading UCI configuration: {e}')
    else:
        print('[WARN] python3-uci is not installed, using default values')
    return cfg

CFG = get_uci_config()
MQTT_BROKER = CFG['mqtt_host']
MQTT_PORT = CFG['mqtt_port']
MQTT_TOPIC_PREFIX = CFG['mqtt_topic']
SERIAL_PORT = CFG['serial_port']
SERIAL_BAUDRATE = CFG['serial_baud']
DEBUG = CFG['debug']
RECONNECT_DELAY = 5  # seconds

# Generalized topic patterns
PIN_TOPIC_SET_WILDCARD = f'{MQTT_TOPIC_PREFIX}/+/set'
PIN_TOPIC_STATE_FMT = f'{MQTT_TOPIC_PREFIX}/{{pin}}/state'

class BridgeDaemon:
    def __init__(self):
        if CallbackAPIVersion is not None:
            self.mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        else:
            self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_connected = False
        self.ser = None
        self.running = True
        self.last_pin_state = {}
        self.kv_store = {}
        self.mailbox = []

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        debug_log(f"[MQTT] Connected with result code {rc}")
        try:
            client.subscribe(PIN_TOPIC_SET_WILDCARD)
            debug_log(f"[MQTT] Subscribed to topic: {PIN_TOPIC_SET_WILDCARD}")
        except Exception as e:
            debug_log(f"[MQTT] Subscribe error: {e}")
        self.mqtt_connected = True

    def on_mqtt_message(self, client, userdata, msg):
        debug_log(f"[MQTT] Message received: {msg.topic} {msg.payload}")
        # Parse pin number from topic: yun/pin/<N>/set
        import re
        m = re.match(rf"{MQTT_TOPIC_PREFIX}/(\d+)/set", msg.topic)
        if m:
            pin = m.group(1)
            payload = msg.payload.decode().strip().upper()
            debug_log(f"[DEBUG] MQTT payload for pin {pin}: {payload}")
            if payload in ('ON', '1'):
                debug_log(f"[DEBUG] Writing 'PIN{pin} ON' to serial")
                if self.ser:
                    self.ser.write(f'PIN{pin} ON\n'.encode())
            elif payload in ('OFF', '0'):
                debug_log(f"[DEBUG] Writing 'PIN{pin} OFF' to serial")
                if self.ser:
                    self.ser.write(f'PIN{pin} OFF\n'.encode())

    def publish_pin_state(self, pin, state):
        if self.mqtt_connected:
            payload = 'ON' if state else 'OFF'
            topic = PIN_TOPIC_STATE_FMT.format(pin=pin)
            self.mqtt_client.publish(topic, payload)
            debug_log(f"[MQTT] Published {payload} to {topic}")

    def handle_command(self, line):
        cmd = line.strip()
        debug_log(f"[DEBUG] Received command: '{cmd}'")
        # Generalized pin ON/OFF/STATE commands
        import re
        m_on = re.match(r'PIN(\d+) ON', cmd)
        m_off = re.match(r'PIN(\d+) OFF', cmd)
        m_state = re.match(r'PIN(\d+) STATE (ON|OFF)', cmd)
        if m_on:
            pin = m_on.group(1)
            debug_log(f"[DEBUG] Action: PIN{pin} ON")
            if self.ser:
                self.ser.write(f'PIN{pin}:ON\n'.encode())
            self.publish_pin_state(pin, True)
            self.last_pin_state[pin] = True
        elif m_off:
            pin = m_off.group(1)
            debug_log(f"[DEBUG] Action: PIN{pin} OFF")
            if self.ser:
                self.ser.write(f'PIN{pin}:OFF\n'.encode())
            self.publish_pin_state(pin, False)
            self.last_pin_state[pin] = False
        elif m_state:
            pin = m_state.group(1)
            state = m_state.group(2)
            debug_log(f"[DEBUG] PIN{pin} state reported by Arduino: {state}")
            self.publish_pin_state(pin, state == 'ON')
        elif cmd.startswith('SET '):
            debug_log(f"[DEBUG] Action: SET (key-value store)")
            try:
                _, key, value = cmd.split(' ', 2)
                self.kv_store[key] = value
                debug_log(f"[DEBUG] Stored: {key} = {value}")
                if self.ser:
                    self.ser.write(f'OK SET {key}\n'.encode())
            except Exception as e:
                debug_log(f"[DEBUG] SET error: {e}")
                if self.ser:
                    self.ser.write(b'ERR SET\n')
        elif cmd.startswith('GET '):
            debug_log(f"[DEBUG] Action: GET (key-value store)")
            try:
                _, key = cmd.split(' ', 1)
                value = self.kv_store.get(key, '')
                debug_log(f"[DEBUG] Retrieved: {key} = {value}")
                if self.ser:
                    self.ser.write(f'VALUE {key} {value}\n'.encode())
            except Exception as e:
                debug_log(f"[DEBUG] GET error: {e}")
                if self.ser:
                    self.ser.write(b'ERR GET\n')
        elif cmd.startswith('RUN '):
            debug_log(f"[DEBUG] Action: RUN (process execution)")
            import subprocess
            try:
                _, command = cmd.split(' ', 1)
                result = subprocess.getoutput(command)
                debug_log(f"[DEBUG] RUN result: {result}")
                if self.ser:
                    self.ser.write(f'RUNOUT {result}\n'.encode())
            except Exception as e:
                debug_log(f"[DEBUG] RUN error: {e}")
                if self.ser:
                    self.ser.write(b'ERR RUN\n')
        elif cmd.startswith('READFILE '):
            debug_log(f"[DEBUG] Action: READFILE")
            try:
                _, path = cmd.split(' ', 1)
                with open(path, 'r') as f:
                    data = f.read(256)
                debug_log(f"[DEBUG] Read from {path}: {data}")
                if self.ser:
                    self.ser.write(f'FILEDATA {data}\n'.encode())
            except Exception as e:
                debug_log(f"[DEBUG] READFILE error: {e}")
                if self.ser:
                    self.ser.write(b'ERR READFILE\n')
        elif cmd.startswith('WRITEFILE '):
            debug_log(f"[DEBUG] Action: WRITEFILE")
            try:
                _, path, data = cmd.split(' ', 2)
                with open(path, 'w') as f:
                    f.write(data)
                debug_log(f"[DEBUG] Wrote to {path}: {data}")
                if self.ser:
                    self.ser.write(b'OK WRITEFILE\n')
            except Exception as e:
                debug_log(f"[DEBUG] WRITEFILE error: {e}")
                if self.ser:
                    self.ser.write(b'ERR WRITEFILE\n')
        elif cmd.startswith('MAILBOX SEND '):
            debug_log(f"[DEBUG] Action: MAILBOX SEND")
            msg = cmd[len('MAILBOX SEND '):]
            self.mailbox.append(msg)
            debug_log(f"[DEBUG] Mailbox appended: {msg}")
            if self.ser:
                self.ser.write(b'OK MAILBOX SEND\n')
        elif cmd == 'MAILBOX RECV':
            debug_log(f"[DEBUG] Action: MAILBOX RECV")
            if self.mailbox:
                msg = self.mailbox.pop(0)
                debug_log(f"[DEBUG] Mailbox popped: {msg}")
                if self.ser:
                    self.ser.write(f'MAILBOX {msg}\n'.encode())
            else:
                debug_log(f"[DEBUG] Mailbox empty")
                if self.ser:
                    self.ser.write(b'MAILBOX EMPTY\n')
        elif cmd.startswith('CONSOLE '):
            msg = cmd[len('CONSOLE '):]
            debug_log(f'[Console] {msg}')
            debug_log(f"[DEBUG] Action: CONSOLE")
            if self.ser:
                self.ser.write(b'OK CONSOLE\n')
        else:
            debug_log(f"[DEBUG] Unknown command")
            if self.ser:
                self.ser.write(b'UNKNOWN COMMAND\n')

    def run(self):
        debug_log(f"[DEBUG] Starting BridgeDaemon run()")
        debug_log(f"[YunBridge v2] Listening on {SERIAL_PORT} @ {SERIAL_BAUDRATE} baud...")
        try:
            debug_log("[DEBUG] Connecting to MQTT broker...")
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_thread = threading.Thread(target=self.mqtt_client.loop_forever, daemon=True)
            mqtt_thread.start()
            debug_log("[DEBUG] MQTT thread started")
            while self.running:
                try:
                    debug_log(f"[DEBUG] Trying to open serial port {SERIAL_PORT}...")
                    with serial.Serial(SERIAL_PORT, SERIAL_BAUDRATE, timeout=1) as ser:
                        self.ser = ser
                        debug_log(f'[INFO] Serial port {SERIAL_PORT} opened')
                        while self.running:
                            try:
                                line = ser.readline().decode(errors='replace').strip()
                                if line:
                                    debug_log(f'[SERIAL] {line}')
                                    self.handle_command(line)
                            except serial.SerialException as e:
                                debug_log(f'[ERROR] Serial port I/O error: {e}')
                                debug_log(f'[INFO] Closing serial port and retrying in {RECONNECT_DELAY} seconds...')
                                self.ser = None
                                try:
                                    ser.close()
                                except Exception:
                                    pass
                                time.sleep(RECONNECT_DELAY)
                                break
                            except Exception as e:
                                debug_log(f'[ERROR] Unexpected error reading from serial port: {e}')
                                time.sleep(1)
                        debug_log(f'[INFO] Serial port {SERIAL_PORT} closed')
                        self.ser = None
                except serial.SerialException as e:
                    debug_log(f'[ERROR] Could not open serial port: {e}')
                    self.ser = None
                    debug_log(f'[INFO] Retrying in {RECONNECT_DELAY} seconds...')
                    time.sleep(RECONNECT_DELAY)
                except Exception as e:
                    debug_log(f'[ERROR] Unexpected error in main loop: {e}')
                    self.ser = None
                    import traceback
                    debug_log(traceback.format_exc())
                    debug_log(f'[INFO] Retrying in {RECONNECT_DELAY} seconds...')
                    time.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            debug_log("[INFO] Daemon stopped by user.")
            self.running = False
        except Exception as e:
            debug_log(f'[FATAL] Unhandled exception in run(): {e}')
            import traceback
            debug_log(traceback.format_exc())
        debug_log('[DEBUG] Exiting BridgeDaemon run()')

if __name__ == '__main__':
    debug_log('[YunBridge] Config used:')
    for k, v in CFG.items():
        debug_log(f'  {k}: {v}')
    daemon = BridgeDaemon()
    daemon.run()
