import re

def main():
    with open('mcubridge/mcubridge/services/runtime.py', 'r') as f:
        content = f.read()

    # 1. Update `state.enqueue_mqtt` to `mqtt_transport.enqueue_mqtt` in SerialHandshakeManager
    content = content.replace("enqueue_mqtt=state.enqueue_mqtt,", "enqueue_mqtt=mqtt_transport.enqueue_mqtt,")

    # 2. Update `self.publish(` to `self.mqtt_flow.publish(`
    content = content.replace("self.publish(", "self.mqtt_flow.publish(")
    
    # 3. Update `self.enqueue_mqtt(` to `self.mqtt_flow.enqueue_mqtt(`
    content = content.replace("self.enqueue_mqtt(", "self.mqtt_flow.enqueue_mqtt(")

    # 4. Remove `async def enqueue_mqtt` and `async def publish` from BridgeService.
    # Use regex to find and remove them.
    for method in ["enqueue_mqtt", "publish"]:
        pattern = re.compile(
            r"([ \t]+)(?:async\s+)?def\s+" + method + r"\s*\([\s\S]*?(?=\n[ \t]+(?:@|def|async|class)\b|\Z)",
            re.MULTILINE
        )
        match = pattern.search(content)
        if match:
            # Check if this is the one inside BridgeService (it is indented)
            method_code = match.group(0)
            content = content.replace(method_code, "")
            
    with open('mcubridge/mcubridge/services/runtime.py', 'w') as f:
        f.write(content)

if __name__ == "__main__":
    main()
