from __future__ import annotations

import pytest
import typer

from tools.emulation import hardware_harness


def test_hardware_harness_requires_core_led_pin_for_led_example() -> None:
    with pytest.raises(typer.BadParameter, match="--led-builtin-pin is required"):
        hardware_harness.run(test_name="led13_test.py")


def test_hardware_harness_does_not_require_led_pin_for_other_examples() -> None:
    assert hardware_harness.requires_led_builtin_pin("console_test.py") is False
