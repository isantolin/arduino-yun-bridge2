"""
This file is part of Arduino Yun Ecosystem v2.

Copyright (C) 2025 Ignacio Santolin and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
#!/usr/bin/env python3
"""
YunBridge v2 Daemon: MQTT <-> Serial bridge for Arduino Yun v2
Refactored to remove redundancy, improve logging, and enhance maintainability.
"""

# Standard library imports
import time
import threading
import atexit
import re
import logging
import json
import subprocess
from logging.handlers import RotatingFileHandler

# Third-party imports
import serial
import paho.mqtt.client as mqtt


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
    'debug': 1,
}

# --- Global Logger Setup (Single Source of Logging) ---
LOG_PATH = '/tmp/yunbridge_daemon.log'
logger = logging.getLogger("yunbridge")
logger.setLevel(logging.INFO)

# Create a stream handler (for console logging) first to capture all messages
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(stream_handler)

logger.debug("Starting bridge_daemon.py")

try:
    logger.debug(f"Attempting to create RotatingFileHandler at {LOG_PATH}")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2000000, backupCount=5)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.debug("RotatingFileHandler created and added successfully")
except Exception as e:
    logger.error(f"Failed to create RotatingFileHandler: {e}")

logger.debug("Logger setup complete")

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
        self.command_response_topic = f'{self.topic_prefix}/command/response'

    def _setup_mqtt_client(self):
        """Initializes and configures the MQTT client (modern paho-mqtt)."""
        self.mqtt_client = mqtt.Client()
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
                # Subscribe to topics with QoS 2
                pin_topic = f'{self.pin_topic_prefix}/+/set'
                mailbox_topic = f'{self.mailbox_topic_prefix}/send'
                command_topic = f'{self.topic_prefix}/command'
                client.subscribe([(pin_topic, 2), (mailbox_topic, 2), (command_topic, 2)])
                logger.info(f"Subscribed to: {pin_topic} (QoS 2)")
                logger.info(f"Subscribed to: {mailbox_topic} (QoS 2)")
                logger.info(f"Subscribed to: {command_topic} (QoS 2)")
            except Exception as e:
                logger.error(f"Failed to subscribe to topics: {e}")
        else:
            logger.error(f"Failed to connect to MQTT broker, return code: {rc}")
            self.mqtt_connected = False

    def on_mqtt_message(self, client, userdata, message):
        topic = message.topic
        try:
            # Ignore messages that are not in our topic prefix
            if not topic.startswith(self.topic_prefix):
                return

            self.logger.debug(f"Received message on topic {topic}: {message.payload}")

            # Handle mailbox messages from MQTT clients
            if topic == f"{self.mailbox_topic_prefix}/send":
                message_str = message.payload.decode('utf-8')
                self.logger.info(f"Forwarding mailbox message from MQTT to MQTT and Serial: {message_str}")
                self.mqtt_client.publish(f"{self.mailbox_topic_prefix}/recv", message_str)
                self._write_to_serial(f"MAILBOX {message_str}\n")

            # Handle pin mode and digital/analog write commands from MQTT
            elif topic.startswith(f"{self.pin_topic_prefix}/"):
                parts = topic.split('/')
                pin_number = parts[2]
                command_type = parts[3] if len(parts) > 3 else None

                if command_type == "mode":
                    mode = message.payload.decode('utf-8').strip().upper()
                    if mode in ["INPUT", "OUTPUT", "PWM"]:
                        self._write_to_serial(f"PIN {pin_number} MODE {mode}\n")
                    else:
                        logger.warning(f"Invalid mode received for PIN {pin_number}: {mode}")
                
                elif command_type in ["digital", "analog"]:
                    value = message.payload.decode('utf-8').strip()
                    self._write_to_serial(f"PIN {pin_number} {command_type.upper()} {value}\n")

            # Add more topic handlers here as needed

        except Exception as e:
            logger.error(f"Error processing MQTT message on topic {topic}: {e}")

    def publish_mqtt(self, topic, payload, retain=False):
        """Publishes a message to an MQTT topic with QoS 2."""
        if self.mqtt_connected:
            self.mqtt_client.publish(topic, payload, qos=2, retain=retain)
            logger.debug(f"Published to MQTT: Topic='{topic}', Payload='{payload}', QoS=2")
        else:
            logger.warning("MQTT not connected. Cannot publish message.")

    # --- Command Handlers (from Serial) ---
    def handle_command(self, line):
        """Parses and dispatches a single command."""
        cmd = line.strip()
        if not cmd:
            return
            
        logger.debug(f"Handling command: '{cmd}'")

        # Regex-based matching for PIN STATE commands from Arduino
        pin_state_match = re.match(r'PIN(\d+) STATE (ON|OFF)', cmd)
        if pin_state_match:
            pin, state = pin_state_match.groups()
            self.command_handlers["PIN_STATE"](pin, state)
            return

        # Space-separated command for all others
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
            logger.warning(f"Unknown command: '{cmd}'")
            # We don't reply to unknown commands to prevent feedback loops.
    
    def _publish_pin_state(self, pin, state_bool):
        """Helper to publish pin state to MQTT."""
        topic = f"{self.pin_topic_prefix}/{pin}/state"
        payload = 'ON' if state_bool else 'OFF'
        self.publish_mqtt(topic, payload)

    def _handle_pin_state(self, pin, state):
        """Handles receiving a pin state and publishing it."""
        self._publish_pin_state(pin, state == 'ON')

    def _handle_set(self, args):
        key, value = args.split(' ', 1)
        self.kv_store[key] = value
        logger.debug(f"Stored in KV: {key} = {value}")
        response = f'OK SET {key}'
        self.publish_mqtt(self.command_response_topic, response)
        self._write_to_serial(f'{response}\n')
        
    def _handle_get(self, key):
        value = self.kv_store.get(key, '')
        logger.debug(f"Retrieved from KV: {key} = {value}")
        response = f'VALUE {key} {value}'
        self.publish_mqtt(self.command_response_topic, response)
        self._write_to_serial(f'{response}\n')
        
    def _handle_run(self, command):
        result = subprocess.getoutput(command)
        logger.debug(f"RUN '{command}' result: {result}")
        response = f'RUNOUT {result}'
        self.publish_mqtt(self.command_response_topic, response)
        self._write_to_serial(f'{response}\n')
        
    def _handle_readfile(self, path):
        try:
            with open(path, 'r') as f:
                data = f.read(256)
            logger.debug(f"Read from {path}: {data}")
            response = f'FILEDATA {data}'
        except Exception as e:
            logger.error(f"Error reading file {path}: {e}")
            response = f'ERR READFILE {e}'
        self.publish_mqtt(self.command_response_topic, response)
        self._write_to_serial(f'{response}\n')

    def _handle_writefile(self, args):
        try:
            path, data = args.split(' ', 1)
            with open(path, 'w') as f:
                f.write(data)
            logger.debug(f"Wrote to {path}: {data}")
            response = 'OK WRITEFILE'
        except Exception as e:
            logger.error(f"Error writing file: {e}")
            response = f'ERR WRITEFILE {e}'
        self.publish_mqtt(self.command_response_topic, response)
        self._write_to_serial(f'{response}\n')

    def _handle_console(self, msg):
        logger.info(f'[Console from Arduino] {msg}')
        response = 'OK CONSOLE'
        self.publish_mqtt(self.command_response_topic, response)
        self._write_to_serial(f'{response}\n')

    def _handle_mailbox_from_serial(self, msg):
        topic = f"{self.mailbox_topic_prefix}/recv"
        self.publish_mqtt(topic, msg)
        logger.info(f"Forwarded mailbox message from Serial to MQTT topic '{topic}'")
        # Send confirmation back to the MCU
        response = f'OK MAILBOX {msg}'
        self._write_to_serial(f'{response}\n')

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
