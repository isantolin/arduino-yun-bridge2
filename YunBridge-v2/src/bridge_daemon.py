#!/usr/bin/env python3
import serial
import time
import paho.mqtt.client as mqtt
import threading
import sys


# Configuración
MQTT_BROKER = 'localhost'
MQTT_PORT = 1883
MQTT_TOPIC_PREFIX = 'yun/bridge'
SERIAL_PORT = '/dev/ttyATH0'
SERIAL_BAUDRATE = 115200
RECONNECT_DELAY = 5  # segundos
PIN_TOPIC_SET = f'{MQTT_TOPIC_PREFIX}/led13/set'
PIN_TOPIC_STATE = f'{MQTT_TOPIC_PREFIX}/led13/state'

class BridgeDaemon:
    def __init__(self):
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_connected = False
        self.ser = None
        self.running = True
        self.last_pin13_state = None
        self.kv_store = {}
        self.mailbox = []

    def on_mqtt_connect(self, client, userdata, flags, rc):
        print(f"[MQTT] Connected with result code {rc}")
        try:
            client.subscribe(PIN_TOPIC_SET)
            print(f"[MQTT] Subscribed to topic: {PIN_TOPIC_SET}")
        except Exception as e:
            print(f"[MQTT] Subscribe error: {e}")
        self.mqtt_connected = True

    def on_mqtt_message(self, client, userdata, msg):
        print(f"[MQTT] Message received: {msg.topic} {msg.payload}")
        if msg.topic == PIN_TOPIC_SET:
            payload = msg.payload.decode().strip().upper()
            print(f"[DEBUG] MQTT payload for LED13: {payload}")
            if payload in ('ON', '1'):
                print("[DEBUG] Writing 'LED13 ON' to serial")
                if self.ser:
                    self.ser.write(b'LED13 ON\n')
            elif payload in ('OFF', '0'):
                print("[DEBUG] Writing 'LED13 OFF' to serial")
                if self.ser:
                    self.ser.write(b'LED13 OFF\n')

    def publish_pin13_state(self, state):
        if self.mqtt_connected:
            payload = 'ON' if state else 'OFF'
            self.mqtt_client.publish(PIN_TOPIC_STATE, payload)
            print(f"[MQTT] Published {payload} to {PIN_TOPIC_STATE}")

    def handle_command(self, line):
        cmd = line.strip()
        print(f"[DEBUG] Received command: '{cmd}'")
        if cmd == 'LED13 ON':
            print("[DEBUG] Action: LED13 ON")
            if self.ser:
                self.ser.write(b'LED13:ON\n')
            self.publish_pin13_state(True)
            self.last_pin13_state = True
        elif cmd == 'LED13 OFF':
            print("[DEBUG] Action: LED13 OFF")
            if self.ser:
                self.ser.write(b'LED13:OFF\n')
            self.publish_pin13_state(False)
            self.last_pin13_state = False
        elif cmd.startswith('LED13 STATE '):
            state = cmd.split(' ', 2)[2]
            print(f"[DEBUG] LED13 state reported by Arduino: {state}")
            self.publish_pin13_state(state == 'ON')
        elif cmd.startswith('SET '):
            print(f"[DEBUG] Action: SET (key-value store)")
            try:
                _, key, value = cmd.split(' ', 2)
                self.kv_store[key] = value
                print(f"[DEBUG] Stored: {key} = {value}")
                if self.ser:
                    self.ser.write(f'OK SET {key}\n'.encode())
            except Exception as e:
                print(f"[DEBUG] SET error: {e}")
                if self.ser:
                    self.ser.write(b'ERR SET\n')
        elif cmd.startswith('GET '):
            print(f"[DEBUG] Action: GET (key-value store)")
            try:
                _, key = cmd.split(' ', 1)
                value = self.kv_store.get(key, '')
                print(f"[DEBUG] Retrieved: {key} = {value}")
                if self.ser:
                    self.ser.write(f'VALUE {key} {value}\n'.encode())
            except Exception as e:
                print(f"[DEBUG] GET error: {e}")
                if self.ser:
                    self.ser.write(b'ERR GET\n')
        elif cmd.startswith('RUN '):
            print(f"[DEBUG] Action: RUN (process execution)")
            import subprocess
            try:
                _, command = cmd.split(' ', 1)
                result = subprocess.getoutput(command)
                print(f"[DEBUG] RUN result: {result}")
                if self.ser:
                    self.ser.write(f'RUNOUT {result}\n'.encode())
            except Exception as e:
                print(f"[DEBUG] RUN error: {e}")
                if self.ser:
                    self.ser.write(b'ERR RUN\n')
        elif cmd.startswith('READFILE '):
            print(f"[DEBUG] Action: READFILE")
            try:
                _, path = cmd.split(' ', 1)
                with open(path, 'r') as f:
                    data = f.read(256)
                print(f"[DEBUG] Read from {path}: {data}")
                if self.ser:
                    self.ser.write(f'FILEDATA {data}\n'.encode())
            except Exception as e:
                print(f"[DEBUG] READFILE error: {e}")
                if self.ser:
                    self.ser.write(b'ERR READFILE\n')
        elif cmd.startswith('WRITEFILE '):
            print(f"[DEBUG] Action: WRITEFILE")
            try:
                _, path, data = cmd.split(' ', 2)
                with open(path, 'w') as f:
                    f.write(data)
                print(f"[DEBUG] Wrote to {path}: {data}")
                if self.ser:
                    self.ser.write(b'OK WRITEFILE\n')
            except Exception as e:
                print(f"[DEBUG] WRITEFILE error: {e}")
                if self.ser:
                    self.ser.write(b'ERR WRITEFILE\n')
        elif cmd.startswith('MAILBOX SEND '):
            print(f"[DEBUG] Action: MAILBOX SEND")
            msg = cmd[len('MAILBOX SEND '):]
            self.mailbox.append(msg)
            print(f"[DEBUG] Mailbox appended: {msg}")
            if self.ser:
                self.ser.write(b'OK MAILBOX SEND\n')
        elif cmd == 'MAILBOX RECV':
            print(f"[DEBUG] Action: MAILBOX RECV")
            if self.mailbox:
                msg = self.mailbox.pop(0)
                print(f"[DEBUG] Mailbox popped: {msg}")
                if self.ser:
                    self.ser.write(f'MAILBOX {msg}\n'.encode())
            else:
                print(f"[DEBUG] Mailbox empty")
                if self.ser:
                    self.ser.write(b'MAILBOX EMPTY\n')
        elif cmd.startswith('CONSOLE '):
            msg = cmd[len('CONSOLE '):]
            print(f'[Console] {msg}')
            print(f"[DEBUG] Action: CONSOLE")
            if self.ser:
                self.ser.write(b'OK CONSOLE\n')
        else:
            print(f"[DEBUG] Unknown command")
            if self.ser:
                self.ser.write(b'UNKNOWN COMMAND\n')

    def run(self):
        print(f"[DEBUG] Iniciando run() de BridgeDaemon")
        print(f"[YunBridge v2] Listening on {SERIAL_PORT} @ {SERIAL_BAUDRATE} baud...")
        try:
            print("[DEBUG] Conectando al broker MQTT...")
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_thread = threading.Thread(target=self.mqtt_client.loop_forever, daemon=True)
            mqtt_thread.start()
            print("[DEBUG] MQTT thread iniciado")
            while self.running:
                try:
                    print(f"[DEBUG] Intentando abrir puerto serie {SERIAL_PORT}...")
                    with serial.Serial(SERIAL_PORT, SERIAL_BAUDRATE, timeout=1) as ser:
                        self.ser = ser
                        print(f'[INFO] Puerto serie {SERIAL_PORT} abierto')
                        while self.running:
                            try:
                                line = ser.readline().decode(errors='replace').strip()
                                if line:
                                    print(f'[SERIAL] {line}')
                                    self.handle_command(line)
                            except serial.SerialException as e:
                                print(f'[ERROR] Error de I/O en el puerto serie: {e}')
                                print(f'[INFO] Cerrando puerto serie y reintentando en {RECONNECT_DELAY} segundos...')
                                self.ser = None
                                try:
                                    ser.close()
                                except Exception:
                                    pass
                                time.sleep(RECONNECT_DELAY)
                                break
                            except Exception as e:
                                print(f'[ERROR] Error inesperado leyendo del puerto serie: {e}')
                                time.sleep(1)
                        print(f'[INFO] Puerto serie {SERIAL_PORT} cerrado')
                        self.ser = None
                except serial.SerialException as e:
                    print(f'[ERROR] No se pudo abrir el puerto serie: {e}')
                    self.ser = None
                    print(f'[INFO] Reintentando en {RECONNECT_DELAY} segundos...')
                    time.sleep(RECONNECT_DELAY)
                except Exception as e:
                    print(f'[ERROR] Error inesperado en el loop principal: {e}')
                    self.ser = None
                    import traceback
                    traceback.print_exc()
                    print(f'[INFO] Reintentando en {RECONNECT_DELAY} segundos...')
                    time.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            print("[INFO] Daemon detenido por el usuario.")
            self.running = False
        except Exception as e:
            print(f'[FATAL] Excepción no controlada en run(): {e}')
            import traceback
            traceback.print_exc()
        print('[DEBUG] Saliendo de run() de BridgeDaemon')

if __name__ == '__main__':
    daemon = BridgeDaemon()
    daemon.run()
