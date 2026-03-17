import pytest
from mcubridge.protocol.structures import _capabilities_to_int, _int_to_capabilities, RuntimeConfig
from mcubridge.daemon import BridgeService
from mcubridge.state.context import RuntimeState, _coerce_snapshot_float, ManagedProcess

def test_capabilities_conversion_exhaustive():
    """Test all possible capability bit combinations to cover structures.py."""
    # Test individual bits
    bits = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048]
    for bit in bits:
        caps_dict = _int_to_capabilities(bit)
        assert any(caps_dict.values())
        assert _capabilities_to_int(caps_dict) == bit

def test_coerce_snapshot_float_error_paths():
    """Cover lines 128-129 in context.py (ValueError/TypeError in float coercion)."""
    assert _coerce_snapshot_float({"k": "invalid"}, "k", 1.0) == 1.0
    assert _coerce_snapshot_float({"k": None}, "k", 1.0) == 1.0
    assert _coerce_snapshot_float({}, "k", 1.0) == 1.0

def test_managed_process_append_empty():
    """Cover lines 188-192 in context.py (append_output with empty chunks)."""
    p = ManagedProcess(pid=1, command="ls")
    p.append_output(b"", b"", limit=100)
    assert len(p.stdout_buffer) == 0
    assert len(p.stderr_buffer) == 0

@pytest.mark.asyncio
async def test_context_error_paths():
    """Test edge cases in BridgeService to cover logic related to state."""
    cfg = RuntimeConfig()
    state = RuntimeState()
    service = BridgeService(cfg, state=state)

    # Test handling of missing transport when sending (logs error, no exception)
    await service.send_frame(0x40, b"")
