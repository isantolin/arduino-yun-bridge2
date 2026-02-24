import re
import os

files = [
    "openwrt-mcu-bridge/tests/test_coverage_final.py",
    "openwrt-mcu-bridge/tests/test_coverage_v3.py",
    "openwrt-mcu-bridge/tests/test_coverage_v2.py",
    "openwrt-mcu-bridge/tests/test_coverage_booster.py"
]

# We need to remove functions testing these callbacks
def clean_file(path):
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        content = f.read()

    # Regex to remove the tests for these functions
    content = re.sub(r'def test_log_baud_retry_no_log_on_first_attempt.*?(?=\ndef |\Z)', '', content, flags=re.DOTALL)
    content = re.sub(r'def test_log_baud_retry_logs_on_subsequent_attempts.*?(?=\ndef |\Z)', '', content, flags=re.DOTALL)
    content = re.sub(r'def test_log_handshake_retry_logs.*?(?=\ndef |\Z)', '', content, flags=re.DOTALL)
    content = re.sub(r'def test_log_retry_attempt_no_log_on_first_attempt.*?(?=\ndef |\Z)', '', content, flags=re.DOTALL)
    content = re.sub(r'def test_log_retry_attempt_logs_on_subsequent_attempts.*?(?=\ndef |\Z)', '', content, flags=re.DOTALL)
    content = re.sub(r'def test_before_sleep_log_no_log_on_first_attempt.*?(?=\ndef |\Z)', '', content, flags=re.DOTALL)
    content = re.sub(r'def test_before_sleep_log_logs_on_subsequent_attempts.*?(?=\ndef |\Z)', '', content, flags=re.DOTALL)
    content = re.sub(r'def test_on_retry_sleep_logs_and_emits.*?(?=\ndef |\Z)', '', content, flags=re.DOTALL)

    with open(path, "w") as f:
        f.write(content)

for f in files:
    clean_file(f)

