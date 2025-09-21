# Try to import boto3 (for Amazon SNS), else set to None
try:
    import boto3
except ImportError:
    boto3 = None
#!/usr/bin/env python3
"""
YunBridge v2 Daemon: MQTT <-> Serial bridge for Arduino Yun v2
Organized and refactored for clarity, robustness, and PEP8 style.
"""

# Standard library imports
import time
import threading
import queue
import os
import atexit
import re


# Third-party imports
import serial
import paho.mqtt.client as mqtt
try:
    from google.cloud import pubsub_v1
except ImportError:
    pubsub_v1 = None

# Try to import CallbackAPIVersion (for newer paho-mqtt), else set to None
try:
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    CallbackAPIVersion = None

# Try to import uci (for OpenWRT config), else set to None
try:
    import uci
except ImportError:
    uci = None

# Async logging globals
_log_buffer = []
_LOG_FILE = '/tmp/yunbridge_debug.log'
# Buffer size: configurable via env, default 50
_LOG_BUFFER_SIZE = int(os.environ.get('YUNBRIDGE_LOG_BUFFER_SIZE', '50'))

# Async logging queue and thread
_log_queue = queue.Queue()
_log_thread = None
_log_thread_running = False


def _log_writer():
    global _log_buffer, _log_thread_running
    _log_thread_running = True
    while _log_thread_running or not _log_queue.empty():
        try:
            line = _log_queue.get(timeout=0.5)
            _log_buffer.append(line)
            if len(_log_buffer) >= _LOG_BUFFER_SIZE:
                with open(_LOG_FILE, 'a') as f:
                    f.writelines(_log_buffer)
                _log_buffer.clear()
        except queue.Empty:
            pass
        except Exception:
            pass
    # Final flush
    if _log_buffer:
        try:
            with open(_LOG_FILE, 'a') as f:
                f.writelines(_log_buffer)
            _log_buffer.clear()
        except Exception:
            pass

def debug_log(msg):
    """Async buffered log to file and optionally to stdout if DEBUG is set."""
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    _log_queue.put(line)
    if globals().get('DEBUG', 0):
        print(line, end='')


def flush_log():
    """Flush any remaining log buffer to disk and stop log thread."""
    global _log_thread_running, _log_thread
    _log_thread_running = False
    if _log_thread:
        _log_thread.join(timeout=2)
    # Final flush handled in _log_writer
def start_log_thread():
    global _log_thread
    if _log_thread is None:
        _log_thread = threading.Thread(target=_log_writer, daemon=True)
        _log_thread.start()

# Start async log thread at import
start_log_thread()
atexit.register(flush_log)


 # Extended options for MQTT security
DEFAULTS = {
    'mqtt_host': '127.0.0.1',
    'mqtt_port': 1883,
    'mqtt_topic': 'yun',
    'mqtt_user': '',
    'mqtt_pass': '',
    'mqtt_tls': 0,  # 0: no TLS, 1: TLS
    'mqtt_cafile': '',
    'mqtt_certfile': '',
    'mqtt_keyfile': '',
    'serial_port': '/dev/ttyATH0',
    'serial_baud': 115200,
    'debug': 0,
    # Pub/Sub options
    'pubsub_enabled': 0,
    'pubsub_project': '',
    'pubsub_topic': '',
    'pubsub_subscription': '',
    'pubsub_credentials': '',
    # Amazon SNS options
    'sns_enabled': 0,
    'sns_region': '',
    'sns_topic_arn': '',
    'sns_access_key': '',
    'sns_secret_key': ''
}



def get_uci_config():
    """Read configuration from UCI using subprocess (robust for OpenWRT)."""
    import subprocess
    cfg = DEFAULTS.copy()
    try:
        # Get all options in the 'main' section of 'yunbridge'
        result = subprocess.run(['uci', 'show', 'yunbridge'], capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            # Example: yunbridge.main.mqtt_host='127.0.0.1'
            parts = line.strip().split('=', 1)
            if len(parts) != 2:
                continue
            key, value = parts
            key_parts = key.split('.')
            if len(key_parts) != 3:
                continue
            _, section, option = key_parts
            if section != 'main':
                continue
            value = value.strip().strip("'\"")
            if option in DEFAULTS:
                if option in ('mqtt_port', 'serial_baud', 'debug', 'mqtt_tls'):
                    try:
                        value = int(value)
                    except Exception:
                        value = DEFAULTS[option]
                cfg[option] = value
    except Exception as e:
        print(f'[WARN] Error reading UCI configuration: {e}')
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
    def publish_sns(self, payload):
        """Publish a message to Amazon SNS topic."""
        if not self.sns_enabled or not self.sns_client or not self.sns_topic_arn:
            return
        try:
            response = self.sns_client.publish(
                TopicArn=self.sns_topic_arn,
                Message=payload
            )
            debug_log(f"[SNS] Published: {payload}")
        except Exception as e:
            debug_log(f"[SNS] Publish error: {e}")
        # Amazon SNS setup
        self.sns_enabled = bool(int(CFG.get('sns_enabled', 0)))
        self.sns_region = CFG.get('sns_region', '')
        self.sns_topic_arn = CFG.get('sns_topic_arn', '')
        self.sns_access_key = CFG.get('sns_access_key', '')
        self.sns_secret_key = CFG.get('sns_secret_key', '')
        self.sns_client = None
        if self.sns_enabled and boto3:
            self.sns_client = boto3.client(
                'sns',
                region_name=self.sns_region,
                aws_access_key_id=self.sns_access_key,
                aws_secret_access_key=self.sns_secret_key
            )
    def start_pubsub(self):
        """Start Pub/Sub subscription listener in a background thread."""
        if not self.pubsub_enabled or not self.pubsub_subscriber or not self.pubsub_subscription:
            return
        import threading
        def callback(message):
            try:
                payload = message.data.decode('utf-8')
                debug_log(f"[PubSub] Message received: {payload}")
                self.handle_pubsub_message(payload)
                message.ack()
            except Exception as e:
                debug_log(f"[PubSub] Error handling message: {e}")
        subscription_path = self.pubsub_subscriber.subscription_path(self.pubsub_project, self.pubsub_subscription)
        thread = threading.Thread(target=self.pubsub_subscriber.subscribe, args=(subscription_path, callback), daemon=True)
        thread.start()

    def handle_pubsub_message(self, payload):
        """Route incoming Pub/Sub message to main handler (same as MQTT)."""
        # For simplicity, treat as MQTT topic 'yun/command' or pin set
        # You can expand this logic to match your topic schema
        if payload.startswith('PIN'):
            self.handle_command(payload)
        else:
            self.handle_command(payload)

    def publish_pubsub(self, payload):
        """Publish a message to Pub/Sub topic."""
        if not self.pubsub_enabled or not self.pubsub_publisher or not self.pubsub_topic:
            return
        topic_path = self.pubsub_publisher.topic_path(self.pubsub_project, self.pubsub_topic)
        future = self.pubsub_publisher.publish(topic_path, payload.encode('utf-8'))
        debug_log(f"[PubSub] Published: {payload}")
    def write_status(self, status, detail=None):
        """Write daemon status to a file for external monitoring."""
        try:
            with open('/tmp/yunbridge_status.json', 'w') as f:
                import json
                data = {'status': status, 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')}
                if detail:
                    data['detail'] = detail
                f.write(json.dumps(data))
        except Exception as e:
            debug_log(f'[WARN] Could not write status file: {e}')
    """
    Main daemon class: handles MQTT, serial, and command processing.
    """
    def __init__(self):
        # MQTT setup
        if CallbackAPIVersion is not None:
            self.mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        else:
            self.mqtt_client = mqtt.Client()
        if CFG.get('mqtt_user'):
            self.mqtt_client.username_pw_set(CFG['mqtt_user'], CFG.get('mqtt_pass', ''))
        if CFG.get('mqtt_tls', 0):
            tls_args = {}
            if CFG.get('mqtt_cafile'):
                tls_args['ca_certs'] = CFG['mqtt_cafile']
            if CFG.get('mqtt_certfile'):
                tls_args['certfile'] = CFG['mqtt_certfile']
            if CFG.get('mqtt_keyfile'):
                tls_args['keyfile'] = CFG['mqtt_keyfile']
            if tls_args:
                self.mqtt_client.tls_set(**tls_args)
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_connected = False
        self.ser = None
        self.running = True
        self.last_pin_state = {}
        self.kv_store = {}
        # Pub/Sub setup
        self.pubsub_enabled = bool(int(CFG.get('pubsub_enabled', 0)))
        self.pubsub_project = CFG.get('pubsub_project', '')
        self.pubsub_topic = CFG.get('pubsub_topic', '')
        self.pubsub_subscription = CFG.get('pubsub_subscription', '')
        self.pubsub_credentials = CFG.get('pubsub_credentials', '')
        self.pubsub_publisher = None
        self.pubsub_subscriber = None
        if self.pubsub_enabled and pubsub_v1:
            import os
            if self.pubsub_credentials:
                os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = self.pubsub_credentials
            self.pubsub_publisher = pubsub_v1.PublisherClient()
            self.pubsub_subscriber = pubsub_v1.SubscriberClient()

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
    # Handle pin set
        m = re.match(rf"{PIN_TOPIC_PREFIX}/(\d+)/set", msg.topic)
        if m:
            pin = m.group(1)
            payload = msg.payload.decode().strip().upper()
            debug_log(f"[DEBUG] MQTT payload for pin {pin}: {payload}")
            if payload in ('ON', '1'):
                debug_log(f"[DEBUG] Writing 'PIN{pin} ON' to serial")
                if self.ser:
                    self.ser.write(f'PIN{pin} ON\n'.encode())
                # Publish to Pub/Sub and SNS as well
                self.publish_pubsub(f'PIN{pin} ON')
                self.publish_sns(f'PIN{pin} ON')
            elif payload in ('OFF', '0'):
                debug_log(f"[DEBUG] Writing 'PIN{pin} OFF' to serial")
                if self.ser:
                    self.ser.write(f'PIN{pin} OFF\n'.encode())
                self.publish_pubsub(f'PIN{pin} OFF')
                self.publish_sns(f'PIN{pin} OFF')
            return
    # Handle mailbox MQTT
        if msg.topic == f"{MAILBOX_TOPIC_PREFIX}/send":
            payload = msg.payload.decode(errors='replace').strip()
            debug_log(f"[MQTT] Mailbox message received: {payload}")
            if self.ser:
                self.ser.write(f'MAILBOX {payload}\n'.encode())
            self.publish_pubsub(f'MAILBOX {payload}')
            self.publish_sns(f'MAILBOX {payload}')
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
    # MAILBOX removed, now uses MQTT
        elif cmd.startswith('CONSOLE '):
            msg = cmd[len('CONSOLE '):]
            debug_log(f'[Console] {msg}')
            debug_log(f"[DEBUG] Action: CONSOLE")
            if self.ser:
                self.ser.write(b'OK CONSOLE\n')
        else:
            # If the command is 'MAILBOX <msg>', publish it to MQTT
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
            self.write_status('starting')
            debug_log("[DEBUG] Connecting to MQTT broker...")
            try:
                self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            except Exception as e:
                debug_log(f'[FATAL] Could not connect to MQTT broker: {e}')
                self.write_status('error', f'MQTT connect failed: {e}')
                return
            mqtt_thread = threading.Thread(target=self.mqtt_client.loop_forever, daemon=True)
            mqtt_thread.start()
            # Start Pub/Sub listener if enabled
            self.start_pubsub()
            debug_log("[DEBUG] MQTT thread started")
            self.write_status('running')
            while self.running:
                try:
                    debug_log(f"[DEBUG] Trying to open serial port {SERIAL_PORT}...")
                    with serial.Serial(SERIAL_PORT, SERIAL_BAUDRATE, timeout=1) as ser:
                        self.ser = ser
                        debug_log(f'[INFO] Serial port {SERIAL_PORT} opened')
                        self.write_status('running', 'serial open')
                        while self.running:
                            try:
                                line = ser.readline().decode(errors='replace').strip()
                                if line:
                                    debug_log(f'[SERIAL] {line}')
                                    self.handle_command(line)
                                    # Publish serial commands to Pub/Sub and SNS for deduplication
                                    self.publish_pubsub(line)
                                    self.publish_sns(line)
                            except serial.SerialException as e:
                                debug_log(f'[ERROR] Serial port I/O error: {e}')
                                debug_log(f'[INFO] Closing serial port and retrying in {RECONNECT_DELAY} seconds...')
                                self.ser = None
                                self.write_status('error', f'Serial I/O error: {e}')
                                try:
                                    ser.close()
                                except Exception:
                                    pass
                                time.sleep(RECONNECT_DELAY)
                                break
                            except Exception as e:
                                debug_log(f'[ERROR] Unexpected error reading from serial port: {e}')
                                self.write_status('error', f'Unexpected serial error: {e}')
                                time.sleep(1)
                        debug_log(f'[INFO] Serial port {SERIAL_PORT} closed')
                        self.ser = None
                        self.write_status('running', 'serial closed')
                except serial.SerialException as e:
                    debug_log(f'[ERROR] Could not open serial port: {e}')
                    self.ser = None
                    self.write_status('error', f'Could not open serial: {e}')
                    debug_log(f'[INFO] Retrying in {RECONNECT_DELAY} seconds...')
                    time.sleep(RECONNECT_DELAY)
                except Exception as e:
                    debug_log(f'[ERROR] Unexpected error in main loop: {e}')
                    self.ser = None
                    self.write_status('error', f'Unexpected main loop error: {e}')
                    import traceback
                    debug_log(traceback.format_exc())
                    debug_log(f'[INFO] Retrying in {RECONNECT_DELAY} seconds...')
                    time.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            debug_log("[INFO] Daemon stopped by user.")
            self.running = False
            self.write_status('stopped', 'KeyboardInterrupt')
        except Exception as e:
            debug_log(f'[FATAL] Unhandled exception in run(): {e}')
            import traceback
            debug_log(traceback.format_exc())
            self.write_status('error', f'Fatal: {e}')
        debug_log('[DEBUG] Exiting BridgeDaemon run()')
        self.write_status('exited')


def main():
    debug_log('[YunBridge] Config used:')
    for k, v in CFG.items():
        debug_log(f'  {k}: {v}')
    daemon = BridgeDaemon()
    try:
        daemon.run()
    finally:
        flush_log()

if __name__ == '__main__':
    main()
