"""Automated dead code detection test using Vulture. [SIL-2]"""

from __future__ import annotations

from pathlib import Path
import vulture

ROOT = Path(__file__).resolve().parents[2]


def test_vulture_zero_dead_code_in_production() -> None:
    """Verify zero dead code in production modules at >= 80% confidence."""
    v = vulture.Vulture()
    targets = [
        str(ROOT / "mcubridge" / "mcubridge"),
        str(ROOT / "mcubridge-client-examples" / "mcubridge_client"),
        str(ROOT / "mcubridge-gateway"),
    ]
    excludes = ["*_pb2.py", "*_pb2.pyi", "*_grpc.py", "openwrt-sdk", ".tox", ".tmp_tests"]
    v.scavenge(targets, exclude=excludes)
    unused = v.get_unused_code(min_confidence=80)
    messages = [f"{item.filename}:{item.first_lineno}: {item.message}" for item in unused]
    assert unused == [], "Dead code detected by Vulture:\n" + "\n".join(messages)


def test_vulture_detects_dead_code_positively() -> None:
    """Positive test: verify Vulture detects dead functions when present."""
    v = vulture.Vulture()
    code = (
        "def _dead_unused_worker():\n"
        "    return 100\n\n"
        "def _active_worker():\n"
        "    return 200\n\n"
        "result = _active_worker()\n"
    )
    v.scan(code, filename="synthetic_test.py")
    unused = v.get_unused_code(min_confidence=60)
    messages = [item.message for item in unused]
    assert any("unused function '_dead_unused_worker'" in m for m in messages)
