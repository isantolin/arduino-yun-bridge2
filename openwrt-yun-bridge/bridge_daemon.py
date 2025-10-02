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
#
# Copyright (c) 2024, Ignacio Santolin
#
# Based on the original work by the Arduino team
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

import sys
import os
import fcntl
import signal
import struct
import logging
import logging.handlers
import argparse
import threading
import time
import collections
import subprocess
import serial
import paho.mqtt.client as mqtt
from yunrpc.frame import Frame
from collections import deque

from yunrpc.protocol import Command, Status, PROTOCOL_VERSION, RPC_BUFFER_SIZE

# Define the constants for the topics
TOPIC_BRIDGE = "br"
TOPIC_DIGITAL = "d"
TOPIC_ANALOG = "a"
TOPIC_CONSOLE = "console"
TOPIC_SH = "sh"

# Global variables
ser = None
client = None

# In-memory dictionary to act as the DataStore
datastore = {}

# In-memory queue for the Mailbox
mailbox_queue = collections.deque()

# Lock for safely accessing the serial port
serial_lock = threading.Lock()

# For Process feature
running_processes = {}
process_lock = threading.Lock()
next_pid = 1

def send_frame(command_id: int, payload: bytes = b'') -> bool:
    """
    Builds and sends a frame to the MCU.
    Returns True if successful, False on error.
    """
    if not ser or not ser.is_open:
        logging.error("Serial port not available for sending.")
        return False

    try:
        frame_bytes = Frame.build(command_id, payload)
        logging.info(f"Enviando frame HEX: {' '.join(f'{b:02X}' for b in frame_bytes)}")
        with serial_lock:
            ser.write(frame_bytes)
        logging.debug(f"LINUX > {Command(command_id).name} PAYLOAD: {payload.hex()}")
        return True
    except serial.SerialException as e:
        logging.error(f"Failed to write to serial port: {e}")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred during send: {e}")
        return False

def on_connect(client, userdata, flags, rc, properties=None):
    """The callback for when the client receives a CONNACK response from the server."""
    if rc == 0:
        logging.info("Connected to MQTT Broker!")
        # Subscribe to specific topics to avoid receiving our own messages
        subscriptions = [
            (f"{TOPIC_BRIDGE}/{TOPIC_DIGITAL}/#", 0),
            (f"{TOPIC_BRIDGE}/{TOPIC_ANALOG}/#", 0),
            (f"{TOPIC_BRIDGE}/{TOPIC_CONSOLE}/in", 0),
            (f"{TOPIC_BRIDGE}/datastore/put/#", 0),
            (f"{TOPIC_BRIDGE}/mailbox/write", 0)
        ]
        client.subscribe(subscriptions)
        for sub in subscriptions:
            logging.info(f"Subscribed to topic: {sub[0]}")
    else:
        logging.error(f"Failed to connect, return code {rc}")


def on_message(client, userdata, msg):
    """The callback for when a PUBLISH message is received from the server."""
    if not ser or not ser.is_open:
        logging.warning("Serial port not ready, skipping MQTT message.")
        return

    logging.info(f"MQTT < {msg.topic} {str(msg.payload)}")
    
    try:
        parts = msg.topic.split('/')
        if len(parts) < 3 or parts[0] != TOPIC_BRIDGE:
            return

        topic_type = parts[1]
        
        if topic_type == TOPIC_CONSOLE and parts[2] == "in":
            send_frame(Command.CMD_CONSOLE_WRITE.value, msg.payload)
            return
        
        # DataStore 'put' from MQTT
        if topic_type == "datastore" and parts[2] == "put":
            if len(parts) > 3:
                key = "/".join(parts[3:])
                value = msg.payload.decode()
                datastore[key] = value
                logging.info(f"DataStore: MQTT set '{key}' to '{value}'")
                # Publish to the 'get' topic for confirmation and state synchronization
                get_topic = f"{TOPIC_BRIDGE}/datastore/get/{key}"
                client.publish(get_topic, value, retain=True)
            return

        # Mailbox 'write' from MQTT
        if topic_type == "mailbox" and parts[2] == "write":
            mailbox_queue.append(msg.payload)
            logging.info(f"Mailbox: Queued message from MQTT ({len(msg.payload)} bytes)")
            # Publish the new count of available messages
            client.publish(f"{TOPIC_BRIDGE}/mailbox/available", str(len(mailbox_queue)), retain=True)
            return

        pin_str = parts[2]
        
        # Digital/Analog Write
        if (topic_type == TOPIC_DIGITAL or topic_type == TOPIC_ANALOG) and len(parts) == 3:
            pin = int(pin_str)
            value = int(msg.payload.decode('utf-8'))
            command = Command.CMD_DIGITAL_WRITE if topic_type == TOPIC_DIGITAL else Command.CMD_ANALOG_WRITE
            payload = struct.pack('<BB', pin, value)
            send_frame(command.value, payload)

        # Pin Mode
        elif topic_type == TOPIC_DIGITAL and len(parts) == 4 and parts[3] == "mode":
            pin = int(pin_str)
            mode = int(msg.payload.decode('utf-8'))
            payload = struct.pack('<BB', pin, mode)
            send_frame(Command.CMD_SET_PIN_MODE.value, payload)

        # Digital/Analog Read Request
        elif (topic_type == TOPIC_DIGITAL or topic_type == TOPIC_ANALOG) and len(parts) == 4 and parts[3] == "read":
            # An MQTT message to this topic triggers a read request to the MCU.
            pin = int(pin_str)
            command = Command.CMD_DIGITAL_READ if topic_type == TOPIC_DIGITAL else Command.CMD_ANALOG_READ
            payload = struct.pack('<B', pin)
            send_frame(command.value, payload)


    except (ValueError, IndexError) as e:
        logging.error(f"Error processing MQTT message: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"An unexpected error occurred in on_message: {e}", exc_info=True)


def handle_mcu_frame(command_id, payload):
    """Handles a command frame received from the MCU."""
    logging.debug(f"MCU > CMD: {Command(command_id).name} PAYLOAD: {payload.hex()}")


    try:
        command = Command(command_id)

        # Handle pin operations by publishing to MQTT
        if command in [Command.CMD_DIGITAL_READ_RESP, Command.CMD_ANALOG_READ_RESP]:
            pin = payload[0]
            value = int.from_bytes(payload[1:], 'little')
            topic_type = TOPIC_DIGITAL if command == Command.CMD_DIGITAL_READ_RESP else TOPIC_ANALOG
            topic = f"{TOPIC_BRIDGE}/{topic_type}/{pin}/value"
            if client:
                client.publish(topic, str(value))
                logging.info(f"Published to {topic}: {value}")
            else:
                logging.warning(f"MQTT client not initialized, cannot publish to {topic}")

        elif command_id == 0x41:
            # MAILBOX_PROCESSED: publish processed message to MQTT
            topic = f"{TOPIC_BRIDGE}/mailbox/processed"
            if client:
                client.publish(topic, payload)
                logging.info(f"Published to {topic}: {payload}")
            else:
                logging.warning(f"MQTT client not initialized, cannot publish to {topic}")

        elif command == Command.CMD_CONSOLE_WRITE:
            logging.info(f"CONSOLE: {payload.decode('utf-8', errors='ignore')}")

        elif command == Command.CMD_DATASTORE_PUT:
            key, value = payload.split(b'\0', 1)
            logging.info(f"Received DATASTORE_PUT for key '{key.decode()}'")
            datastore[key.decode()] = value.decode('utf-8')

        elif command == Command.CMD_DATASTORE_GET:
            key = payload.decode('utf-8')
            logging.info(f"Received DATASTORE_GET for key '{key}'")
            value = datastore.get(key, "")
            send_frame(Command.CMD_DATASTORE_GET_RESP.value, value.encode('utf-8'))

        elif command == Command.CMD_MAILBOX_READ:
            logging.info("Received MAILBOX_READ")
            message = b""
            if mailbox_queue:
                message = mailbox_queue.popleft()
            send_frame(Command.CMD_MAILBOX_READ_RESP.value, message)

        elif command == Command.CMD_MAILBOX_AVAILABLE:
            logging.info("Received MAILBOX_AVAILABLE")
            available_count = len(mailbox_queue)
            logging.info(f"Mailbox queue has {available_count} messages.")
            available_payload = str(available_count).encode('utf-8')
            send_frame(Command.CMD_MAILBOX_AVAILABLE_RESP.value, available_payload)

        elif command == Command.CMD_FILE_WRITE:
            logging.warning(f"FILE_WRITE command is not fully implemented.")

        elif command == Command.CMD_PROCESS_RUN:
            cmd_str = payload.decode('utf-8')
            logging.info(f"Received PROCESS_RUN for command: '{cmd_str}'")
            try:
                result = subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=10)
                response = result.stdout
                logging.info(f"Command stdout: {response}")
                send_frame(Command.CMD_PROCESS_RUN_RESP.value, response.encode('utf-8'))
            except Exception as e:
                logging.error(f"Error running process '{cmd_str}': {e}")
                send_frame(Command.CMD_PROCESS_RUN_RESP.value, str(e).encode('utf-8'))

        elif command == Command.CMD_PROCESS_RUN_ASYNC:
            global next_pid
            cmd_str = payload.decode('utf-8')
            logging.info(f"Received PROCESS_RUN_ASYNC for command: '{cmd_str}'")
            try:
                process = subprocess.Popen(cmd_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                with process_lock:
                    pid = next_pid
                    running_processes[pid] = process
                    next_pid += 1
                logging.info(f"Started process '{cmd_str}' with internal PID {pid} (OS PID {process.pid})")
                send_frame(Command.CMD_PROCESS_RUN_ASYNC_RESP.value, str(pid).encode('utf-8'))
            except Exception as e:
                logging.error(f"Error running async process '{cmd_str}': {e}")
                send_frame(Command.CMD_PROCESS_RUN_ASYNC_RESP.value, b"0")

        elif command == Command.CMD_PROCESS_POLL:
            pid_str = payload.decode('utf-8')
            pid = int(pid_str)
            logging.info(f"Received PROCESS_POLL for PID {pid}")
            output = ""
            with process_lock:
                if pid in running_processes:
                    process = running_processes[pid]
                    try:
                        output = process.stdout.read()
                    except Exception as e:
                        output = f"Error reading stdout for PID {pid}: {e}"
                    if process.poll() is not None:
                        logging.info(f"Process with PID {pid} has finished. Cleaning up.")
                        del running_processes[pid]
                else:
                    output = f"No process found with PID {pid}"
            logging.info(f"Polling PID {pid}, output: '{output}'")
            send_frame(Command.CMD_PROCESS_POLL_RESP.value, output.encode('utf-8'))

        elif command == Command.CMD_PROCESS_KILL:
            pid_str = payload.decode('utf-8')
            pid = int(pid_str)
            logging.info(f"Received PROCESS_KILL for PID {pid}")
            with process_lock:
                if pid in running_processes:
                    try:
                        running_processes[pid].kill()
                        logging.info(f"Killed process with PID {pid}")
                        del running_processes[pid]
                    except Exception as e:
                        logging.error(f"Error killing process PID {pid}: {e}")
                else:
                    logging.warning(f"Attempted to kill non-existent PID {pid}")
        else:
            logging.warning(f"Unknown command received: {command}")
            send_frame(Status.CMD_UNKNOWN.value, b'')

    except (ValueError, IndexError, UnicodeDecodeError, struct.error) as e:
        logging.error(f"Error processing frame: {e}")
        try:
            send_frame(Status.ERROR.value, str(e).encode('utf-8'))
        except Exception as e2:
            logging.error(f"Could not even send error response: {e2}")


def main():
    """Main function."""
    global ser, client

    parser = argparse.ArgumentParser(description="Arduino Yun Bridge Daemon")
    parser.add_argument('--verbose', action='store_true', help="Print logs to console instead of syslog")
    parser.add_argument('--ip', type=str, default='192.168.15.28', help='MQTT broker IP address')
    parser.add_argument('--serial-port', type=str, default='/dev/ttyATH0', help='Serial port for MCU communication')
    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    if args.verbose:
        logging.basicConfig(level=log_level, format=log_format)
    else:
        logging.basicConfig(level=log_level, filename='/var/log/yun-bridge.log', filemode='a', format=log_format)

    logging.info("Starting yun-bridge daemon.")

    # Ensure the script runs as a single instance
    pid_file_path = "/var/run/yun-bridge.pid"
    pid_file = None
    try:
        pid_file = open(pid_file_path, "w")
        fcntl.lockf(pid_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_file.write(str(os.getpid()))
        pid_file.flush()
    except IOError:
        logging.error("Another instance of yun-bridge is running. Exiting.")
        sys.exit(1)

    # Setup MQTT client
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(args.ip, 1883, 60)
    except Exception as e:
        logging.error(f"Can't connect to MQTT broker at {args.ip}: {e}")
        sys.exit(1)
    client.loop_start()

    serial_buffer = b''
    from yunrpc import protocol

    # Main loop
    while True:
        try:
            if not ser or not ser.is_open:
                logging.info(f"Attempting to connect to serial port {args.serial_port}...")
                try:
                    ser = serial.Serial(args.serial_port, 115200, timeout=1)
                    ser.reset_input_buffer()
                    logging.info("Serial port connected successfully.")
                except serial.SerialException as e:
                    logging.warning(f"Serial port not ready: {e}. Retrying in 5 seconds...")
                    ser = None
                    time.sleep(5)
                    continue

            if ser.in_waiting > 0:
                with serial_lock:
                    serial_buffer += ser.read(ser.in_waiting)
            
            # Process the buffer to find and parse frames
            buffer_modified = True
            while buffer_modified:
                buffer_modified = False
                start_index = serial_buffer.find(protocol.START_BYTE)

                if start_index == -1:
                    if len(serial_buffer) > 0:
                        logging.warning(f"Discarding {len(serial_buffer)} bytes of junk data: {serial_buffer.hex()}")
                        serial_buffer = b''
                    break

                if start_index > 0:
                    logging.warning(f"Discarding {start_index} bytes of junk data before frame: {serial_buffer[:start_index].hex()}")
                    serial_buffer = serial_buffer[start_index:]
                    buffer_modified = True
                    continue

                if len(serial_buffer) < protocol.MIN_FRAME_SIZE:
                    break

                try:
                    _, payload_len, _ = struct.unpack(protocol.CRC_COVERED_HEADER_FORMAT, serial_buffer[1:1+protocol.CRC_COVERED_HEADER_SIZE])
                except struct.error:
                    logging.warning("Could not unpack header, discarding start byte.")
                    serial_buffer = serial_buffer[1:]
                    buffer_modified = True
                    continue

                full_frame_len = 1 + protocol.CRC_COVERED_HEADER_SIZE + payload_len + protocol.CRC_SIZE
                
                if len(serial_buffer) < full_frame_len:
                    break

                frame_bytes = serial_buffer[:full_frame_len]
                
                try:
                    command_id, payload = Frame.parse(frame_bytes)
                    handle_mcu_frame(command_id, payload)
                    serial_buffer = serial_buffer[full_frame_len:]
                    buffer_modified = True

                except ValueError as e:
                    logging.warning(f"Frame parsing error: {e}. Discarding start byte and rescanning.")
                    serial_buffer = serial_buffer[1:]
                    buffer_modified = True
            
            time.sleep(0.01)

        except (serial.SerialException, IOError) as e:
            logging.error(f"Serial communication error: {e}")
            if ser:
                ser.close()
            ser = None
            time.sleep(5)
        
        except Exception as e:
            logging.critical(f"Unhandled exception in main loop: {e}", exc_info=True)
            break

    # Cleanup
    logging.info("Shutting down yun-bridge.")
    if client:
        client.loop_stop()
    if ser and ser.is_open:
        ser.close()
    
    if pid_file:
        fcntl.lockf(pid_file, fcntl.LOCK_UN)
        pid_file.close()
        try:
            os.remove(pid_file_path)
        except OSError as e:
            logging.error(f"Error removing pid file: {e}")

    sys.exit(0)


if __name__ == "__main__":
    main()
