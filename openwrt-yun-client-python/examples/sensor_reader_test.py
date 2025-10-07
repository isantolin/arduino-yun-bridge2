import argparse
import time
import paho.mqtt.client as mqtt

# --- Configuration ---
# The pin to read. Use a format like 'd13' for digital or 'a0' for analog.
PIN_TO_READ = "d13" 
# PIN_TO_READ = "a0" 

# How often to request a reading (in seconds)
READ_INTERVAL = 2

# --- MQTT Topics ---
# Topic to publish read requests to
# The daemon expects br/d/{pin}/read or br/a/{pin}/read
REQUEST_TOPIC = f"br/{PIN_TO_READ[0]}/{PIN_TO_READ[1:]}/read"

# Topic to subscribe to for receiving the pin's value
# The daemon publishes responses to br/d/{pin}/value or br/a/{pin}/value
VALUE_TOPIC = f"br/{PIN_TO_READ[0]}/{PIN_TO_READ[1:]}/value"

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback for when the client connects to the broker."""
    if rc == 0:
        print("Connected to MQTT Broker!")
        # Subscribe to the topic where the pin's value will be published
        client.subscribe(VALUE_TOPIC)
        print(f"Subscribed to topic: {VALUE_TOPIC}")
    else:
        print(f"Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    """Callback for when a message is received from the broker."""
    # This is where we receive the value from the pin
    print(f"Received value for pin {PIN_TO_READ}: {msg.payload.decode()}")

def main():
    parser = argparse.ArgumentParser(description="Yun Bridge Sensor Reading Test")
    parser.add_argument('--ip', type=str, required=True, help="The IP address of the Arduino Yun.")
    parser.add_argument('--port', type=int, default=1883, help="The MQTT port of the Arduino Yun.")
    args = parser.parse_args()

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.ip, args.port, 60)
    except Exception as e:
        print(f"Error connecting to MQTT broker: {e}")
        return

    # Start the MQTT client loop in a background thread
    client.loop_start()

    print(f"Requesting a reading from pin {PIN_TO_READ} every {READ_INTERVAL} seconds.")
    print("Press Ctrl+C to exit.")

    try:
        while True:
            # Publish a message to the request topic. The payload doesn't matter.
            print(f"Sending read request to {REQUEST_TOPIC}")
            client.publish(REQUEST_TOPIC, "read")
            time.sleep(READ_INTERVAL)
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()
