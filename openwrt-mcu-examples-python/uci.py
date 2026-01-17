import os


class Uci:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def get_all(self, package, section):
        if package == "mcubridge" and section == "general":
            return {
                "mqtt_host": os.environ.get("MQTT_HOST", "192.168.15.36"),
                "mqtt_port": os.environ.get("MQTT_PORT", "8883"),
                "mqtt_tls": "1",
                "mqtt_tls_insecure": "1",
                "mqtt_user": os.environ.get("MQTT_USER", "ignacio.santolin"),
                "mqtt_pass": os.environ.get("MQTT_PASS", "placeholder_password"),
            }
        return {}
