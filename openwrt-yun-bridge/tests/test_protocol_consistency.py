
import io
import sys
import tomllib
from pathlib import Path

# Add the project root to sys.path so we can import tools
PROJECT_ROOT = Path(__file__).parents[2]
sys.path.append(str(PROJECT_ROOT))

from tools.protocol.generate import generate_python, generate_cpp  # noqa: E402


def test_protocol_python_is_up_to_date():
    """Ensure generated Python protocol matches the spec."""
    spec_path = PROJECT_ROOT / "tools/protocol/spec.toml"
    py_path = PROJECT_ROOT / "openwrt-yun-bridge/yunbridge/rpc/protocol.py"

    with open(spec_path, "rb") as f:
        spec = tomllib.load(f)

    output = io.StringIO()
    generate_python(spec, output)
    generated_content = output.getvalue()

    with open(py_path, "r") as f:
        current_content = f.read()

    assert generated_content == current_content, (
        "Python protocol definition is out of sync with spec.toml. "
        "Run 'tools/protocol/generate.py' to update."
    )


def test_protocol_cpp_is_up_to_date():
    """Ensure generated C++ protocol matches the spec."""
    spec_path = PROJECT_ROOT / "tools/protocol/spec.toml"
    cpp_path = PROJECT_ROOT / "openwrt-library-arduino/src/protocol/rpc_protocol.h"

    with open(spec_path, "rb") as f:
        spec = tomllib.load(f)

    output = io.StringIO()
    generate_cpp(spec, output)
    generated_content = output.getvalue()

    with open(cpp_path, "r") as f:
        current_content = f.read()

    assert generated_content == current_content, (
        "C++ protocol definition is out of sync with spec.toml. "
        "Run 'tools/protocol/generate.py' to update."
    )
