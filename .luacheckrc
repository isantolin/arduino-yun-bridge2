std = "lua51"
globals = {
    "luci", "nixio", "uci", "sys", "posix", "module", "package", "io", "os", "math", "require", "pairs", "ipairs", "tostring", "tonumber", "print", "unpack", "table", "string",
    "translate", "Map", "NamedSection", "TypedSection", "Value", "ListValue", "Flag", "DummyValue", "SimpleSection", "entry", "template", "call", "cbi", "index",
    "action_api", "action_mqtt_ws_auth", "action_mqtt_ws_url", "action_status", "action_rotate_credentials", "action_hw_smoke"
}
exclude_files = {
    "node_modules/**"
}
