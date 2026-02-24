import re
import os

files_to_patch = [
    "openwrt-mcu-bridge/mcubridge/services/payloads.py",
    "openwrt-mcu-bridge/mcubridge/state/status.py"
]

for filepath in files_to_patch:
    with open(filepath, "r") as f:
        content = f.read()

    # In msgspec 0.18+, gc parameter was removed from decode/Decoder, 
    # the correct usage is generally not to pass gc=False anymore as it's default behavior, 
    # but since the prompt asked for "Recolección de Basura Desactivada (gc=False) En msgspec.json.decode()", 
    # we need to be careful. Wait, msgspec doesn't support gc parameter in this version (as tested). 
    # Let me check msgspec version in the project.
    pass

