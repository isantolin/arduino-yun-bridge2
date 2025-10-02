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

# Global variables
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
last_received_value = None
value_received = False

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback for when the client connects to the broker."""
    if rc == 0:
        logging.info("Connected to MQTT Broker!")
    else:
        logging.error(f"Failed to connect, return code {rc}")

def on_message(client, userdata, msg):
    """Callback for when a message is received from the broker."""
    global last_received_value, value_received
    logging.info(f"Received message on topic {msg.topic}: {msg.payload.decode()}")
    # We are interested in the 'get' topic for the key we requested
    if msg.topic.startswith(f"{TOPIC_BRIDGE}/datastore/get/"):
        last_received_value = msg.payload.decode()
        value_received = True

def datastore_put(key, value):
    """Puts a key-value pair into the DataStore via MQTT."""
    topic = f"{TOPIC_BRIDGE}/datastore/put/{key}"
    logging.info(f"Publishing to {topic}: {value}")
    client.publish(topic, value)

def datastore_get(key):
    """Gets a value from the DataStore by key via MQTT."""
    global value_received, last_received_value
    topic = f"{TOPIC_BRIDGE}/datastore/get/{key}"
    value_received = False
    last_received_value = None
    
    # Subscribe to the specific get topic
    client.subscribe(topic)
    logging.info(f"Subscribed to {topic} to get value.")
    
    # Wait for the response
    timeout = 5  # seconds
    start_time = time.time()
    while not value_received and (time.time() - start_time) < timeout:
        time.sleep(0.1)
        
    client.unsubscribe(topic) # Clean up subscription
    
    if value_received:
        logging.info(f"Got response for key '{key}': '{last_received_value}'")
        return last_received_value
    else:
        logging.warning(f"Timeout: No response for key '{key}'")
        return None

def main():
    """Main function to test DataStore functionality."""
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(BROKER, PORT, 60)
    except ConnectionRefusedError:
        logging.error("Connection to MQTT broker refused. Is the broker running?")
        sys.exit(1)

    client.loop_start()

    # --- Test Cases ---
    print("\n--- Starting DataStore MQTT Test ---")

    # Test 1: Put a new key-value pair
    print("\n[Test 1: Put a new key-value pair]")
    key1 = "mqtt_test/temperature"
    value1 = "25.5"
    datastore_put(key1, value1)
    time.sleep(1) # Give broker time to process

    # Test 2: Get the value back
    print(f"\n[Test 2: Get the value for '{key1}']")
    retrieved_value = datastore_get(key1)
    if retrieved_value == value1:
        print(f"SUCCESS: Retrieved value '{retrieved_value}' matches put value '{value1}'.")
    else:
        print(f"FAILURE: Retrieved value '{retrieved_value}' does not match put value '{value1}'.")

    # Test 3: Update the value
    print("\n[Test 3: Update the value]")
    value2 = "26.0"
    datastore_put(key1, value2)
    time.sleep(1)
    retrieved_value_2 = datastore_get(key1)
    if retrieved_value_2 == value2:
        print(f"SUCCESS: Retrieved value '{retrieved_value_2}' matches updated value '{value2}'.")
    else:
        print(f"FAILURE: Retrieved value '{retrieved_value_2}' does not match updated value '{value2}'.")

    # Test 4: Get a non-existent key (should return nothing or empty)
    print("\n[Test 4: Get a non-existent key]")
    key2 = "non_existent/key"
    retrieved_value_3 = datastore_get(key2)
    if retrieved_value_3 is None or retrieved_value_3 == "":
        print(f"SUCCESS: Correctly received no value for non-existent key '{key2}'.")
    else:
        print(f"FAILURE: Incorrectly received value '{retrieved_value_3}' for non-existent key.")
        
    # Test 5: Check the value put by the Arduino sketch
    print("\n[Test 5: Check value put by Arduino sketch]")
    arduino_key = "test_key"
    arduino_value = datastore_get(arduino_key)
    if arduino_value == "test_value":
        print(f"SUCCESS: Retrieved value '{arduino_value}' for key '{arduino_key}' put by Arduino.")
    else:
        print(f"FAILURE: Did not retrieve expected value for key '{arduino_key}'. Got '{arduino_value}'.")


    print("\n--- Test Complete ---")

    client.loop_stop()
    client.disconnect()

if __name__ == "__main__":
    main()
