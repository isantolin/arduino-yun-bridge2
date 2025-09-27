#!/usr/bin/env python3
"""
CGI script for YunWebUI v2 REST generic pin control
Expects POST /arduino-webui-v2/pin?pin=N&state=ON|OFF
Controls any digital pin. The 'pin' parameter is required (no default).
"""
import os
import sys
import serial
import time
import subprocess
import logging
import re
import json
from logging.handlers import RotatingFileHandler

# --- Logger Setup (reuse yunbridge global logger style) ---
LOG_PATH = '/tmp/yunbridge_daemon.log'
logger = logging.getLogger("yunbridge")
logger.setLevel(logging.INFO)
file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2000000, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
if not logger.hasHandlers():
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

# --- UCI Config Loader (reuse from bridge_daemon.py) ---
DEFAULTS = {
    'serial_port': '/dev/ttyATH0',
    'serial_baud': 115200,
}
def get_uci_config():
    cfg = DEFAULTS.copy()
    try:
        result = subprocess.run(['uci', 'show', 'yunbridge'], capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            match = re.match(r"yunbridge\.main\.(\w+)='?([^']*)'?", line.strip())
            if match:
                key, value = match.groups()
                if key in DEFAULTS:
                    if key == 'serial_baud':
                        try:
                            cfg[key] = int(value)
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid integer value for UCI key '{key}': '{value}'. Using default.")
                    else:
                        cfg[key] = value
        logger.info('UCI configuration loaded successfully (CGI).')
    except FileNotFoundError:
        logger.warning('`uci` command not found. Using default configuration (CGI).')
    except subprocess.CalledProcessError:
        logger.warning('No UCI configuration found for `yunbridge`. Using default configuration (CGI).')
    except Exception as e:
        logger.error(f'Error reading UCI configuration (CGI): {e}')
    return cfg

CFG = get_uci_config()
SERIAL_PORT = CFG['serial_port']
BAUDRATE = CFG['serial_baud']


def parse_query(query):
    params = {}
    for part in query.split('&'):
        if '=' in part:
            k, v = part.split('=', 1)
            params[k] = v
    return params

def get_pin_from_path():
    # Expect PATH_INFO like /pin/13
    path = os.environ.get('PATH_INFO', '')
    m = re.match(r"/pin/(\d+)$", path)
    if m:
        return m.group(1)
    return None



def send_response(status_code, data):
    print(f'Status: {status_code}')
    print('Content-Type: application/json\n')
    print(json.dumps(data))

def main():
    method = os.environ.get('REQUEST_METHOD', 'GET').upper()
    pin = get_pin_from_path()
    logger.info(f"REST call: method={method}, pin={pin}")
    if not pin or not pin.isdigit():
        logger.error('Failed: pin parameter missing or invalid.')
        send_response(400, {
            'status': 'error',
            'message': 'Pin must be specified in the URL as /pin/<N>.'
        })
        return

    if method == 'GET':
        # GET pin status
        try:
            with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=2) as ser:
                ser.reset_input_buffer()
                ser.write(f'PIN{pin} STATUS\n'.encode())
                time.sleep(0.1)
                raw = ser.readline().decode(errors='ignore').strip()
            logger.info(f'Pin {pin} status response: {raw}')
            m = re.match(r'PIN(\d+)=([A-Z]+)', raw)
            if m:
                pin_num, pin_state = m.groups()
                send_response(200, {
                    'status': 'ok',
                    'pin': int(pin_num),
                    'state': pin_state,
                    'message': f'Pin {pin_num} is {pin_state}'
                })
            else:
                send_response(502, {
                    'status': 'error',
                    'message': f'Unexpected response: {raw}'
                })
        except Exception as e:
            logger.error(f'Error getting status: {e} (pin {pin})')
            send_response(500, {
                'status': 'error',
                'message': f'Failed to get status for pin {pin}: {e}'
            })
        return

    elif method == 'POST':
        # POST: set pin state, expect JSON body {"state": "ON"}
        try:
            content_length = int(os.environ.get('CONTENT_LENGTH', 0))
            body = sys.stdin.read(content_length) if content_length > 0 else ''
            data = json.loads(body) if body else {}
            state = data.get('state', '').upper()
        except Exception as e:
            logger.error(f'POST body parse error: {e}')
            send_response(400, {
                'status': 'error',
                'message': 'Invalid JSON body.'
            })
            return
        if state not in ('ON', 'OFF'):
            send_response(400, {
                'status': 'error',
                'message': 'State must be "ON" or "OFF".'
            })
            return
        try:
            with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
                ser.write(f'PIN{pin} {state}\n'.encode())
            logger.info(f'Success: Pin {pin} turned {state}')
            send_response(200, {
                'status': 'ok',
                'pin': int(pin),
                'state': state,
                'message': f'Pin {pin} turned {state}'
            })
        except Exception as e:
            logger.error(f'Error: {e} (pin {pin})')
            send_response(500, {
                'status': 'error',
                'message': f'Failed to control pin {pin}: {e}'
            })
        return

    else:
        send_response(405, {
            'status': 'error',
            'message': f'Method {method} not allowed.'
        })

if __name__ == '__main__':
    main()
