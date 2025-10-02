import argparse
import threading
import time
import paho.mqtt.client as mqtt

# MQTT Topics
TOPIC_CONSOLE_IN = "br/console/in"
TOPIC_CONSOLE_OUT = "br/console/out"

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback for when the client connects to the broker."""
    if rc == 0:
        print("Connected to MQTT Broker!")
        client.subscribe(TOPIC_CONSOLE_OUT)
        print(f"Subscribed to topic: {TOPIC_CONSOLE_OUT}")
    else:
        print(f"Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    """Callback for when a message is received from the broker."""
    print(f"Received from Arduino: {msg.payload.decode()}")

def input_thread(client):
    """Thread to handle user input and publish to the console topic."""
    print("\nEnter text to send to the Arduino console. Type 'exit' to quit.")
    while True:
        try:
            message = input()
            if message.lower() == 'exit':
                break
            client.publish(TOPIC_CONSOLE_IN, message)
        except EOFError:
            # This can happen if the input stream is closed, e.g., in a script
            break
    client.loop_stop()

def main():
    parser = argparse.ArgumentParser(description="Yun Bridge Console Test")
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

    # Start the input thread
    thread = threading.Thread(target=input_thread, args=(client,))
    thread.daemon = True
    thread.start()

    # Start the MQTT loop
    client.loop_start()

    # Wait for the input thread to finish (i.e., user types 'exit')
    thread.join()
    print("Exiting...")

if __name__ == "__main__":
    main()
