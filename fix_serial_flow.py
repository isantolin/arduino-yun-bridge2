import re
path = "openwrt-mcu-bridge/mcubridge/services/serial_flow.py"
with open(path, "r") as f:
    text = f.read()

# The issue is that we deleted _on_retry_sleep which emitted the 'retry' metric.
# We need to wrap before_sleep to both log and emit the metric.
# Or better, we can restore the metric emission by using a custom function that delegates to before_sleep_log.

replacement = """
    def _on_retry_sleep(self, retry_state: tenacity.RetryCallState) -> None:
        self._emit_metric("retry")
        tenacity.before_sleep_log(self._logger, logging.WARNING)(retry_state)
"""

text = text.replace(
    "before_sleep=tenacity.before_sleep_log(self._logger, logging.WARNING),",
    "before_sleep=self._on_retry_sleep,"
)

# Insert the function back
text = re.sub(
    r'(def _build_retryer\(self\) -> tenacity\.AsyncRetrying:\n.*?(?=\n    def ))',
    r'\g<1>\n' + replacement.lstrip("\n") + '\n',
    text,
    flags=re.DOTALL
)

with open(path, "w") as f:
    f.write(text)
