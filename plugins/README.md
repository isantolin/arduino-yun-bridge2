# Community Plugins via MQTT

This folder contains example community plugins for the Arduino Yun v2 ecosystem. Plugins are independent scripts or services that interact with the YunBridge system using MQTT topics.

## How Plugins Work
- Plugins subscribe to MQTT topics (e.g., `yun/pin/+/state`, `yun/command`, custom topics).
- Plugins can publish commands or data to the same broker, affecting the YunBridge daemon, Arduino, or other clients.
- No changes to the daemon or sketch are required: plugins are fully decoupled and communicate only via MQTT.

## Example Use Cases
- Automation: Turn on a pin when a sensor triggers.
- Notification: Send an email or Telegram message when a pin changes state.
- Integration: Bridge Yun with other home automation systems (Home Assistant, Node-RED, etc).

## Example Plugin: Auto Toggle Pin 13
See `auto_toggle_pin13.py` for a simple example that toggles pin 13 every 10 seconds using MQTT.

## Example: Auto Toggle Pin 13

An example plugin is provided: `auto_toggle_pin13.py`.

This script toggles pin 13 ON and OFF every 10 seconds using MQTT. You can use it as a template for your own automations.

**Usage:**

1. Edit `auto_toggle_pin13.py` to set your MQTT broker address, port, and credentials if needed.
2. Run the plugin on your OpenWRT device or any machine with network access to the broker:

```bash
python3 auto_toggle_pin13.py
```

You should see output indicating the ON/OFF state being published to the topic `yun/pin/13/set`.

**How it works:**

- Publishes `ON` and `OFF` alternately to the MQTT topic `yun/pin/13/set` every 10 seconds.
- The Yun Bridge Daemon will receive these messages and control the pin accordingly.

Feel free to copy and modify this script to create your own automations!

## How to Create a Plugin
1. Write a script in Python, Bash, Node.js, etc. that connects to the MQTT broker.
2. Subscribe to the topics you want to monitor.
3. Publish commands or data as needed.
4. Place your script in this `plugins/` folder and document its usage.

## Example Topics
- `yun/pin/<N>/set` (control a pin)
- `yun/pin/<N>/state` (monitor pin state)
- `yun/command` (send advanced commands)
- `yun/mailbox/send` and `yun/mailbox/recv` (arbitrary messages)

## Contributing
- Share your plugin by submitting a pull request or opening an issue with your script and description.
- Plugins should be well-documented and not require changes to the core daemon or sketch.

---

This system enables powerful, decoupled extensions to YunBridge using only MQTT. Experiment and share your automations!
