import io
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
sys.path.append(str(PROJECT_ROOT))

from tools.protocol.generate import (  # noqa: E402
    CppGenerator,
    ProtocolSpec,
    PythonGenerator,
)


def test_protocol_python_is_up_to_date(tmp_path):
    """Ensure generated Python protocol matches the spec."""
    spec_path = PROJECT_ROOT / "tools/protocol/spec.toml"
    py_path = PROJECT_ROOT / "openwrt-mcu-bridge/mcubridge/protocol/protocol.py"

    spec = ProtocolSpec.load(spec_path)

    output = io.StringIO()
    PythonGenerator().generate(spec, output)

    # Save to a temporary file to run ruff format on it
    tmp_py = tmp_path / "protocol.py"
    tmp_py.write_text(output.getvalue(), encoding="utf-8")

    # Run ruff format on the temporary file
    subprocess.run(["ruff", "format", str(tmp_py)], check=True, capture_output=True)
    generated_content = tmp_py.read_text(encoding="utf-8")

    with open(py_path, "r") as f:
        current_content = f.read()

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
