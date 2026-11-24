"""Regression tests for Wave A Pigweed pw_log/pw_tokenizer & pw_bloat telemetry."""

from __future__ import annotations

from pathlib import Path

import pytest
from pw_hdlc.encode import ui_frame
from pw_tokenizer.encode import encode_token_and_args
from pw_tokenizer.tokens import pw_tokenizer_65599_hash

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport
from tools.emulation.emulation_runner import resolve_firmware_token_database, setup_fake_uci
from tools.emulation.stack_metrics import extract_latest_mcu_stack_watermark


def test_resolve_firmware_token_database_priority(tmp_path: Path) -> None:
    """Prefer sibling tokens.csv over firmware.elf, and return None when absent."""
    fw_bin = tmp_path / "firmware_flash.bin"
    fw_bin.write_bytes(b"\x00")
    assert resolve_firmware_token_database(fw_bin) is None

    fw_elf = tmp_path / "firmware.elf"
    fw_elf.write_bytes(b"\x7fELF")
    assert resolve_firmware_token_database(fw_bin) == fw_elf
    assert resolve_firmware_token_database(fw_elf) == fw_elf

    empty_csv = tmp_path / "tokens.csv"
    empty_csv.write_text("", encoding="utf-8")
    assert resolve_firmware_token_database(fw_bin) == fw_elf

    empty_csv.write_text('f5db307c,          ,"","BRIDGE_SAFE_STATE"\n', encoding="utf-8")
    assert resolve_firmware_token_database(fw_bin) == empty_csv
    assert resolve_firmware_token_database(fw_elf) == empty_csv


def test_setup_fake_uci_exports_token_database(tmp_path: Path) -> None:
    """Verify setup_fake_uci propagates MCUBRIDGE_TOKEN_DATABASE when provided."""
    tokens_csv = tmp_path / "tokens.csv"
    tokens_csv.write_text('f5db307c,          ,"","BRIDGE_SAFE_STATE"\n', encoding="utf-8")
    _, _, daemon_env = setup_fake_uci(
        "/dev/null",
        device_id="token-test",
        base_dir=tmp_path / "node",
        token_database=tokens_csv,
    )
    assert daemon_env["MCUBRIDGE_TOKEN_DATABASE"] == str(tokens_csv)


@pytest.mark.asyncio
async def test_tokenized_stack_watermark_roundtrip_to_stack_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify tokenized MCU_STACK_WATERMARK logs detokenize and parse cleanly."""
    fmt = "MCU_STACK_WATERMARK arch=SAMD21 used=%u budget=%u"
    token = pw_tokenizer_65599_hash(fmt)
    tokens_csv = tmp_path / "tokens.csv"
    tokens_csv.write_text(f'{token:08x},          ,"","{fmt}"\n', encoding="utf-8")
    monkeypatch.setenv("MCUBRIDGE_TOKEN_DATABASE", str(tokens_csv))

    state = RuntimeState()
    try:
        transport = SerialTransport(RuntimeConfig(), state, None)
        payload = encode_token_and_args(token, 1824, 26112)
        decoded = transport.decode_log_payload(payload)
        assert decoded == "MCU_STACK_WATERMARK arch=SAMD21 used=1824 budget=26112"

        watermark = extract_latest_mcu_stack_watermark([f'2026-11-23 [info] MCU log log="{decoded}"'])
        assert watermark is not None
        assert watermark["arch"] == "SAMD21"
        assert watermark["stack_used_bytes"] == 1824
        assert watermark["stack_budget_bytes"] == 26112
        assert watermark["stack_margin_bytes"] == 26112 - 1824

        # Also exercise full HDLC channel decoding without raising.
        await transport.feed_data(ui_frame(protocol.HDLC_ADDRESS_LOG, payload))
    finally:
        state.cleanup()
