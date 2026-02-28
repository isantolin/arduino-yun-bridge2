import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
sys.path.append(str(PROJECT_ROOT))

from tools.protocol.generate import (  # noqa: E402
    CppGenerator,
    ProtocolSpec,
    PythonGenerator,
)


def _normalize_python_content(content: str) -> str:
    """Normalize Python content for comparison by stripping whitespace and ignoring empty lines."""
    lines = content.splitlines()
    normalized = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            normalized.append(stripped)
    return "\n".join(normalized)


def test_protocol_python_is_up_to_date():
    """Ensure generated Python protocol matches the spec."""
    spec_path = PROJECT_ROOT / "tools/protocol/spec.toml"
    py_path = PROJECT_ROOT / "openwrt-mcu-bridge/mcubridge/protocol/protocol.py"

    spec = ProtocolSpec.load(spec_path)

    output = io.StringIO()
    PythonGenerator().generate(spec, output)

    generated_content = _normalize_python_content(output.getvalue())

    with open(py_path, "r") as f:
        current_content = _normalize_python_content(f.read())

    assert generated_content == current_content, (
        "Python protocol definition is out of sync with spec.toml. " "Run 'tools/protocol/generate.py' to update."
    )


def test_protocol_cpp_is_up_to_date():
    """Ensure generated C++ protocol matches the spec."""
    spec_path = PROJECT_ROOT / "tools/protocol/spec.toml"
    cpp_path = PROJECT_ROOT / "openwrt-library-arduino/src/protocol/rpc_protocol.h"

    spec = ProtocolSpec.load(spec_path)

    output = io.StringIO()
    CppGenerator().generate_header(spec, output)
    generated_content = output.getvalue()

    with open(cpp_path, "r") as f:
        current_content = f.read()

    assert generated_content == current_content, (
        "C++ protocol definition is out of sync with spec.toml. " "Run 'tools/protocol/generate.py' to update."
    )
