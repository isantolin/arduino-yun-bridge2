from yunbridge.config import credentials


def test_lookup_credential_prefers_blank_env_over_file():
    result = credentials.lookup_credential(
        ("YUNBRIDGE_MQTT_USER",),
        credential_map={"YUNBRIDGE_MQTT_USER": "fromfile"},
        environ={"YUNBRIDGE_MQTT_USER": "   "},
        fallback="fallback",
    )

    assert result == ""


def test_lookup_credential_allows_blank_file_value():
    result = credentials.lookup_credential(
        ("YUNBRIDGE_MQTT_USER",),
        credential_map={"YUNBRIDGE_MQTT_USER": "   "},
        environ={},
        fallback="fallback",
    )

    assert result == ""
