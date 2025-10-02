import paho.mqtt.client as mqtt
import time
import sys
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# MQTT settings
BROKER = "localhost"
PORT = 1883
TOPIC_BRIDGE = "br"

def main():
    """Main function to send messages to the Mailbox."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    try:
        client.connect(BROKER, PORT, 60)
    except ConnectionRefusedError:
        logging.error("Connection to MQTT broker refused. Is the broker running?")
        sys.exit(1)

    client.loop_start()

    if len(sys.argv) < 2:
        print("Usage: python3 mailbox_test.py <message>")
        print("Example: python3 mailbox_test.py 'led:1'")
        sys.exit(1)

    message = " ".join(sys.argv[1:])
    topic = f"{TOPIC_BRIDGE}/mailbox/write"

    logging.info(f"Publishing to topic '{topic}': '{message}'")
    client.publish(topic, message)

    # Give the message time to be sent and processed
    time.sleep(1)

    client.loop_stop()
    client.disconnect()
    logging.info("Message sent and client disconnected.")

if __name__ == "__main__":
    main()
