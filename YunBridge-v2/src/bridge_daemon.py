
#!/usr/bin/env python3
"""
YunBridge v2 Daemon: MQTT <-> Serial bridge for Arduino Yun v2
Organizado y refactorizado para claridad, robustez y estilo PEP8.
"""

import os
import sys
import time
import threading
import serial
import paho.mqtt.client as mqtt
import re
import subprocess
try:
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    CallbackAPIVersion = None
try:
    import uci
except ImportError:
    uci = None

DEFAULTS = {
    'mqtt_host': '127.0.0.1',
    'mqtt_port': 1883,
    'mqtt_topic': 'yun',
    'serial_port': '/dev/ttyATH0',
    'serial_baud': 115200,
    'debug': 0
}

def debug_log(msg):
    """Log to file and optionally to stdout if DEBUG is set."""
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    try:
        with open('/tmp/yunbridge_debug.log', 'a') as f:
            f.write(line)
    except Exception:
        pass
    if globals().get('DEBUG', 0):
        print(line, end='')

def get_uci_config():
    """Read configuration from UCI or use defaults."""
    cfg = DEFAULTS.copy()
    if uci is not None:
        try:
            c = uci.UCI()
            section = None
            # Try to find the section by introspection (API is inconsistent)
            for attr in dir(c):
                obj = getattr(c, attr)
                if isinstance(obj, dict) and 'yunbridge' in obj:
                    yb = obj['yunbridge']
                    if isinstance(yb, dict) and 'main' in yb:
                        section = yb['main']
                        break
            if section:
                for k in DEFAULTS:
                    v = section.get(k)
                    if v is not None:
                        if k in ('mqtt_port', 'serial_baud', 'debug'):
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
PIN_TOPIC_PREFIX = f'{MQTT_TOPIC_PREFIX}/pin'
MAILBOX_TOPIC_PREFIX = f'{MQTT_TOPIC_PREFIX}/mailbox'
SERIAL_PORT = CFG['serial_port']
SERIAL_BAUDRATE = CFG['serial_baud']
DEBUG = CFG['debug']
RECONNECT_DELAY = 5  # seconds

# Generalized topic patterns
PIN_TOPIC_SET_WILDCARD = f'{PIN_TOPIC_PREFIX}/+/set'
PIN_TOPIC_STATE_FMT = f'{PIN_TOPIC_PREFIX}/{{pin}}/state'


class BridgeDaemon:
    """
    Main daemon class: handles MQTT, serial, and command processing.
    """
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

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        """MQTT on_connect callback."""
        debug_log(f"[MQTT] Connected with result code {rc}")
        try:
            client.subscribe(PIN_TOPIC_SET_WILDCARD)
            debug_log(f"[MQTT] Subscribed to topic: {PIN_TOPIC_SET_WILDCARD}")
            client.subscribe(f"{MAILBOX_TOPIC_PREFIX}/send")
            debug_log(f"[MQTT] Subscribed to topic: {MAILBOX_TOPIC_PREFIX}/send")
        except Exception as e:
            debug_log(f"[MQTT] Subscribe error: {e}")
        self.mqtt_connected = True

    def on_mqtt_message(self, client, userdata, msg):
        """MQTT on_message callback."""
        debug_log(f"[MQTT] Message received: {msg.topic} {msg.payload}")
        # Manejo de pin set
        m = re.match(rf"{PIN_TOPIC_PREFIX}/(\d+)/set", msg.topic)
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
            return
        # Manejo de mailbox MQTT
        if msg.topic == f"{MAILBOX_TOPIC_PREFIX}/send":
            payload = msg.payload.decode(errors='replace').strip()
            debug_log(f"[MQTT] Mailbox message received: {payload}")
            if self.ser:
                self.ser.write(f'MAILBOX {payload}\n'.encode())
            return

    def publish_pin_state(self, pin, state):
        """Publish pin state to MQTT."""
        if self.mqtt_connected:
            payload = 'ON' if state else 'OFF'
            topic = PIN_TOPIC_STATE_FMT.format(pin=pin)
            self.mqtt_client.publish(topic, payload)
            debug_log(f"[MQTT] Published {payload} to {topic}")

    def handle_command(self, line):
        """Parse and execute a command received from serial."""
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
        # MAILBOX eliminado, ahora se usa MQTT
        elif cmd.startswith('CONSOLE '):
            msg = cmd[len('CONSOLE '):]
            debug_log(f'[Console] {msg}')
            debug_log(f"[DEBUG] Action: CONSOLE")
            if self.ser:
                self.ser.write(b'OK CONSOLE\n')
        else:
            # Si el comando es 'MAILBOX <msg>', publícalo en MQTT
            if cmd.startswith('MAILBOX '):
                msg = cmd[len('MAILBOX '):]
                self.publish_mailbox_message(msg)
                debug_log(f"[DEBUG] Forwarded MAILBOX to MQTT: {msg}")
            else:
                debug_log(f"[DEBUG] Unknown command")
                if self.ser:
                    self.ser.write(b'UNKNOWN COMMAND\n')

    def publish_mailbox_message(self, msg):
        """Publish a mailbox message to MQTT."""
        if self.mqtt_connected:
            topic = f"{MAILBOX_TOPIC_PREFIX}/recv"
            self.mqtt_client.publish(topic, msg)
            debug_log(f"[MQTT] Published mailbox message to {topic}: {msg}")

    def run(self):
        """Main loop: handles MQTT and serial communication."""
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
