import paho.mqtt.client as mqtt
import time
import argparse

# Define the constants for the topics
TOPIC_BRIDGE = "br"
TOPIC_DIGITAL = "d"

def main():
    parser = argparse.ArgumentParser(description="Simple MQTT blink test for Yun Bridge")
    parser.add_argument('--ip', required=True, help="IP address of the MQTT broker on the Yun")
    args = parser.parse_args()

    client = mqtt.Client(client_id="BlinkTest")


    
    print(f"Connecting to MQTT broker at {args.ip}...")
    try:
        client.connect(args.ip, 1883, 60)
    except Exception as e:
        print(f"Error connecting to MQTT broker: {e}")
        return

    client.loop_start()
    time.sleep(1) # Wait for connection

    pin = 13
    topic = f"{TOPIC_BRIDGE}/{TOPIC_DIGITAL}/{pin}"

    print(f"Starting to blink pin {pin}...")
    try:
        for i in range(5):
            # Turn ON
            print(f"MQTT > {topic} : 1 (ON)")
            client.publish(topic, "1")
            time.sleep(1)

            # Turn OFF
            print(f"MQTT > {topic} : 0 (OFF)")
            client.publish(topic, "0")
            time.sleep(1)
    except KeyboardInterrupt:
        print("Blinking stopped by user.")
    finally:
        # Ensure pin is left off
        client.publish(topic, "0")
        time.sleep(0.5)
        client.loop_stop()
        client.disconnect()
        print("Disconnected.")

if __name__ == "__main__":
    main()
