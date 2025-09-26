#!/usr/bin/env python3
"""
YunBridge v2 Daemon: MQTT <-> Serial bridge for Arduino Yun v2
Refactored to remove redundancy, improve logging, and enhance maintainability.
"""

# Standard library imports
import time
import threading
import os
import atexit
import re
import logging
import json
import subprocess
from logging.handlers import RotatingFileHandler

# Third-party imports
import serial
import paho.mqtt.client as mqtt

# Try to import CallbackAPIVersion (for newer paho-mqtt)
try:
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    CallbackAPIVersion = None

# --- Configuration ---
DEFAULTS = {
    'mqtt_host': '127.0.0.1',
    'mqtt_port': 1883,
    'mqtt_topic': 'yun',
    'mqtt_user': '',
    'mqtt_pass': '',
    'mqtt_tls': 0,
    'mqtt_cafile': '',
    'mqtt_certfile': '',
    'mqtt_keyfile': '',
    'serial_port': '/dev/ttyATH0',
    'serial_baud': 115200,
    'debug': 0,
}

# --- Global Logger Setup (Single Source of Logging) ---
LOG_PATH = '/tmp/yunbridge_daemon.log'
logger = logging.getLogger("yunbridge")
logger.setLevel(logging.INFO)

# Create a rotating file handler (for file logging)
file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2000000, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

# Create a stream handler (for console logging)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

# Add handlers to the logger (avoid duplicates)
if not logger.hasHandlers():
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

# --- Configuration Loading ---
def get_uci_config():
    """Loads configuration from OpenWRT's UCI system."""
    cfg = DEFAULTS.copy()
    try:
        logger.debug('Reading UCI configuration for yunbridge.')
        result = subprocess.run(['uci', 'show', 'yunbridge'], capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            match = re.match(r"yunbridge\.main\.(\w+)='?([^']*)'?", line.strip())
            if match:
                key, value = match.groups()
                if key in DEFAULTS:
                    # Type conversion for specific keys
                    if key in ('mqtt_port', 'serial_baud', 'debug', 'mqtt_tls'):
                        try:
                            cfg[key] = int(value)
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid integer value for UCI key '{key}': '{value}'. Using default.")
                    else:
                        cfg[key] = value
        logger.info('UCI configuration loaded successfully.')
    except FileNotFoundError:
        logger.warning('`uci` command not found. Using default configuration.')
    except subprocess.CalledProcessError:
        logger.warning('No UCI configuration found for `yunbridge`. Using default configuration.')
    except Exception as e:
        logger.error(f'Error reading UCI configuration: {e}')
    return cfg

def validate_config(cfg):
    """Validates that essential configuration keys are present."""
    required = ['mqtt_host', 'mqtt_port', 'mqtt_topic', 'serial_port', 'serial_baud']
    for key in required:
        if not cfg.get(key):
            raise ValueError(f"Config error: '{key}' is required and missing or empty.")
    if cfg.get('mqtt_tls') and not cfg.get('mqtt_cafile'):
        raise ValueError("Config error: mqtt_tls enabled but mqtt_cafile not set.")

# Load and validate config at startup
CFG = get_uci_config()
validate_config(CFG)
if CFG['debug']:
    logger.setLevel(logging.DEBUG)

# --- Main Daemon Class ---
class BridgeDaemon:
    """
    Main daemon class: handles MQTT, serial, and command processing.
    """
    def __init__(self, config):
        self.cfg = config
        self.ser = None
        self.running = True
        self.mqtt_connected = False
        self.kv_store = {}
        self.reconnect_delay = 5  # seconds

        # Setup command dispatcher
        self.command_handlers = {
            "PIN_ON": self._handle_pin_on,
            "PIN_OFF": self._handle_pin_off,
            "PIN_STATE": self._handle_pin_state,
            "SET": self._handle_set,
            "GET": self._handle_get,
            "RUN": self._handle_run,
            "READFILE": self._handle_readfile,
            "WRITEFILE": self._handle_writefile,
            "CONSOLE": self._handle_console,
            "MAILBOX": self._handle_mailbox_from_serial,
        }

        # Setup MQTT client
        self._setup_mqtt_client()
        
        # Topic prefixes
        self.topic_prefix = self.cfg['mqtt_topic']
        self.pin_topic_prefix = f'{self.topic_prefix}/pin'
        self.mailbox_topic_prefix = f'{self.topic_prefix}/mailbox'

    def _setup_mqtt_client(self):
        """Initializes and configures the MQTT client."""
        client_args = {}
        if CallbackAPIVersion is not None:
            client_args['callback_api_version'] = CallbackAPIVersion.VERSION2
        
        self.mqtt_client = mqtt.Client(**client_args)
        
        if self.cfg.get('mqtt_user'):
            self.mqtt_client.username_pw_set(self.cfg['mqtt_user'], self.cfg.get('mqtt_pass', ''))
        
        if self.cfg.get('mqtt_tls'):
            try:
                self.mqtt_client.tls_set(
                    ca_certs=self.cfg.get('mqtt_cafile'),
                    certfile=self.cfg.get('mqtt_certfile'),
                    keyfile=self.cfg.get('mqtt_keyfile')
                )
            except Exception as e:
                logger.error(f"Failed to set up MQTT TLS: {e}")

        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

    # --- Helper Methods ---
    def _write_to_serial(self, data):
        """Safely writes data to the serial port if it's open."""
        if self.ser and self.ser.is_open:
            try:
                if isinstance(data, str):
                    data = data.encode('utf-8')
                self.ser.write(data)
                logger.debug(f"Wrote to serial: {data.strip()}")
            except serial.SerialException as e:
                self._handle_serial_error("Failed to write to serial port", e)
        else:
            logger.warning(f"Serial port not open. Could not write: {data.strip()}")

    def _handle_serial_error(self, message, exception):
        """Centralized handler for serial connection errors."""
        logger.error(f"{message}: {exception}")
        if self.ser:
            try:
                self.ser.close()
            except Exception as e:
                logger.error(f"Error while closing serial port: {e}")
        self.ser = None
        self.write_status('error', f'Serial error: {exception}')

    def write_status(self, status, detail=None):
        """Write daemon status to a file for external monitoring."""
        try:
            with open('/tmp/yunbridge_status.json', 'w') as f:
                data = {'status': status, 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')}
                if detail:
                    data['detail'] = detail
                f.write(json.dumps(data))
        except Exception as e:
            logger.warning(f'Could not write status file: {e}')

    # --- MQTT Callbacks and Methods ---
    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        """Callback for when the client connects to the MQTT broker."""
        if rc == 0:
            logger.info("Successfully connected to MQTT broker.")
            self.mqtt_connected = True
            try:
                # Subscribe to topics
                pin_topic = f'{self.pin_topic_prefix}/+/set'
                mailbox_topic = f'{self.mailbox_topic_prefix}/send'
                client.subscribe([(pin_topic, 0), (mailbox_topic, 0)])
                logger.info(f"Subscribed to: {pin_topic}")
                logger.info(f"Subscribed to: {mailbox_topic}")
            except Exception as e:
                logger.error(f"Failed to subscribe to topics: {e}")
        else:
            logger.error(f"Failed to connect to MQTT broker, return code: {rc}")
            self.mqtt_connected = False

    def on_mqtt_message(self, client, userdata, msg):
        """Callback for when a message is received from the MQTT broker."""
        logger.debug(f"MQTT message received: Topic='{msg.topic}', Payload='{msg.payload.decode()}'")
        try:
            # Handle pin set commands
            pin_match = re.match(rf"{self.pin_topic_prefix}/(\d+)/set", msg.topic)
            if pin_match:
                pin = pin_match.group(1)
                payload = msg.payload.decode().strip().upper()
                if payload in ('ON', '1'):
                    self._write_to_serial(f'PIN{pin} ON\n')
                elif payload in ('OFF', '0'):
                    self._write_to_serial(f'PIN{pin} OFF\n')
                return

            # Handle mailbox messages
            if msg.topic == f"{self.mailbox_topic_prefix}/send":
                payload = msg.payload.decode(errors='replace').strip()
                self._write_to_serial(f'MAILBOX {payload}\n')
                return
        except Exception as e:
            logger.error(f"Error processing MQTT message on topic {msg.topic}: {e}")

    def publish_mqtt(self, topic, payload, retain=False):
        """Publishes a message to an MQTT topic."""
        if self.mqtt_connected:
            self.mqtt_client.publish(topic, payload, retain=retain)
            logger.debug(f"Published to MQTT: Topic='{topic}', Payload='{payload}'")
        else:
            logger.warning("MQTT not connected. Cannot publish message.")

    # --- Command Handlers (from Serial) ---
    def handle_command(self, line):
        """Parses and dispatches one or more commands received from serial (tolerant to glued/concatenated commands)."""
        raw = line.strip()
        logger.debug(f"Received from serial: '{raw}'")
        # Split on 'PIN' but keep the delimiter (for glued commands)
        # Also handle possible 'MAILBOX', 'SET', etc. at start
        tokens = re.split(r'(?=PIN\d+ )', raw)
        for token in tokens:
            cmd = token.strip()
            if not cmd:
                continue
            # Regex-based matching for PIN commands for flexibility
            pin_on_match = re.match(r'PIN(\d+) ON', cmd)
            pin_off_match = re.match(r'PIN(\d+) OFF', cmd)
            pin_state_match = re.match(r'PIN(\d+) STATE (ON|OFF)', cmd)
            if pin_on_match:
                self.command_handlers["PIN_ON"](pin_on_match.group(1))
            elif pin_off_match:
                self.command_handlers["PIN_OFF"](pin_off_match.group(1))
            elif pin_state_match:
                self.command_handlers["PIN_STATE"](pin_state_match.group(1), pin_state_match.group(2))
            else:
                # Space-separated command for others
                parts = cmd.split(' ', 1)
                command_key = parts[0]
                args = parts[1] if len(parts) > 1 else ""
                handler = self.command_handlers.get(command_key)
                if handler:
                    try:
                        handler(args)
                    except Exception as e:
                        logger.error(f"Error executing command '{command_key}': {e}")
                        self._write_to_serial(f'ERR {command_key}\n')
                else:
                    logger.warning(f"Unknown command received from serial: '{cmd}'")
                    self._write_to_serial('UNKNOWN COMMAND\n')
    
    def _publish_pin_state(self, pin, state):
        """Helper to publish pin state to MQTT."""
        topic = f"{self.pin_topic_prefix}/{pin}/state"
        payload = 'ON' if state else 'OFF'
        self.publish_mqtt(topic, payload)

    def _handle_pin_on(self, pin):
        self._write_to_serial(f'PIN{pin}:ON\n')
        self._publish_pin_state(pin, True)
    
    def _handle_pin_off(self, pin):
        self._write_to_serial(f'PIN{pin}:OFF\n')
        self._publish_pin_state(pin, False)

    def _handle_pin_state(self, pin, state):
        self._publish_pin_state(pin, state == 'ON')

    def _handle_set(self, args):
        key, value = args.split(' ', 1)
        self.kv_store[key] = value
        logger.debug(f"Stored in KV: {key} = {value}")
        self._write_to_serial(f'OK SET {key}\n')
        
    def _handle_get(self, key):
        value = self.kv_store.get(key, '')
        logger.debug(f"Retrieved from KV: {key} = {value}")
        self._write_to_serial(f'VALUE {key} {value}\n')
        
    def _handle_run(self, command):
        result = subprocess.getoutput(command)
        logger.debug(f"RUN '{command}' result: {result}")
        self._write_to_serial(f'RUNOUT {result}\n')
        
    def _handle_readfile(self, path):
        with open(path, 'r') as f:
            data = f.read(256)
        logger.debug(f"Read from {path}: {data}")
        self._write_to_serial(f'FILEDATA {data}\n')

    def _handle_writefile(self, args):
        path, data = args.split(' ', 1)
        with open(path, 'w') as f:
            f.write(data)
        logger.debug(f"Wrote to {path}: {data}")
        self._write_to_serial('OK WRITEFILE\n')

    def _handle_console(self, msg):
        logger.info(f'[Console from Arduino] {msg}')
        self._write_to_serial('OK CONSOLE\n')

    def _handle_mailbox_from_serial(self, msg):
        topic = f"{self.mailbox_topic_prefix}/recv"
        self.publish_mqtt(topic, msg)

    # --- Main Execution Loop ---
    def run(self):
        """Main loop: handles MQTT and serial communication."""
        logger.info(f"Starting YunBridge v2 Daemon...")
        logger.info(f"Listening on {self.cfg['serial_port']} @ {self.cfg['serial_baud']} baud.")
        self.write_status('starting')

        try:
            logger.info(f"Connecting to MQTT broker at {self.cfg['mqtt_host']}:{self.cfg['mqtt_port']}")
            self.mqtt_client.connect(self.cfg['mqtt_host'], self.cfg['mqtt_port'], 60)
            mqtt_thread = threading.Thread(target=self.mqtt_client.loop_forever, daemon=True)
            mqtt_thread.start()
        except Exception as e:
            logger.critical(f'Could not connect to MQTT broker: {e}')
            self.write_status('error', f'MQTT connect failed: {e}')
            return

        while self.running:
            try:
                logger.info(f"Attempting to open serial port {self.cfg['serial_port']}...")
                with serial.Serial(self.cfg['serial_port'], self.cfg['serial_baud'], timeout=1) as ser:
                    self.ser = ser
                    logger.info(f'Serial port {self.cfg["serial_port"]} opened successfully.')
                    self.write_status('running', 'serial open')
                    while self.running:
                        try:
                            line = ser.readline().decode(errors='replace').strip()
                            if line:
                                self.handle_command(line)
                        except serial.SerialException as e:
                            self._handle_serial_error("Serial port I/O error", e)
                            break # Break inner loop to retry opening the port
                        except Exception as e:
                            logger.error(f"Unexpected error reading from serial: {e}")
                            time.sleep(1)
                
                # This block runs if serial port closes gracefully or after an error
                logger.warning("Serial port closed.")
                self.write_status('running', 'serial closed')
                self.ser = None
                
            except serial.SerialException as e:
                self._handle_serial_error("Could not open serial port", e)
            except Exception as e:
                logger.critical(f"Unexpected critical error in main loop: {e}", exc_info=True)
                self.write_status('error', 'critical main loop error')
            
            if self.running:
                logger.info(f"Retrying serial connection in {self.reconnect_delay} seconds...")
                time.sleep(self.reconnect_delay)

    def stop(self):
        """Stops the daemon gracefully."""
        logger.info("Stopping daemon...")
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.mqtt_client.disconnect()
        self.write_status('stopped')

def main():
    logger.debug('Configuration being used:')
    for k, v in CFG.items():
        logger.debug(f'  {k}: {v}')
        
    daemon = BridgeDaemon(CFG)
    
    def shutdown_handler():
        daemon.stop()

    atexit.register(shutdown_handler)

    try:
        daemon.run()
    except KeyboardInterrupt:
        logger.info("Daemon stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Unhandled fatal exception in main: {e}", exc_info=True)
    finally:
        daemon.stop()

if __name__ == '__main__':
    main()
