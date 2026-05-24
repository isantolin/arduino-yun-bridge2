import os
import re
from pathlib import Path

MAPPING = {
    "pb.VersionResponse": "pb.VersionResponse",
    "pb.FreeMemoryResponse": "pb.FreeMemoryResponse",
    "pb.Capabilities": "pb.Capabilities",
    "pb.PinMode": "pb.PinMode",
    "pb.DigitalWrite": "pb.DigitalWrite",
    "pb.AnalogWrite": "pb.AnalogWrite",
    "pb.PinRead": "pb.PinRead",
    "pb.DigitalReadResponse": "pb.DigitalReadResponse",
    "pb.AnalogReadResponse": "pb.AnalogReadResponse",
    "pb.ConsoleWrite": "pb.ConsoleWrite",
    "pb.DatastorePut": "pb.DatastorePut",
    "pb.DatastoreGet": "pb.DatastoreGet",
    "pb.DatastoreGetResponse": "pb.DatastoreGetResponse",
    "pb.MailboxPush": "pb.MailboxPush",
    "pb.MailboxProcessed": "pb.MailboxProcessed",
    "pb.MailboxAvailableResponse": "pb.MailboxAvailableResponse",
    "pb.MailboxReadResponse": "pb.MailboxReadResponse",
    "pb.FileWrite": "pb.FileWrite",
    "pb.FileRead": "pb.FileRead",
    "pb.FileRemove": "pb.FileRemove",
    "pb.FileReadResponse": "pb.FileReadResponse",
    "pb.ProcessRunAsync": "pb.ProcessRunAsync",
    "pb.ProcessRunAsyncResponse": "pb.ProcessRunAsyncResponse",
    "pb.ProcessPoll": "pb.ProcessPoll",
    "pb.ProcessPollResponse": "pb.ProcessPollResponse",
    "pb.ProcessKill": "pb.ProcessKill",
    "pb.pb.AckPacket": "pb.pb.pb.AckPacket",
    "pb.HandshakeConfig": "pb.HandshakeConfig",
    "pb.pb.SetBaudratePacket": "pb.pb.pb.SetBaudratePacket",
    "pb.LinkSync": "pb.LinkSync",
    "pb.EnterBootloader": "pb.EnterBootloader",
    "pb.SpiTransfer": "pb.SpiTransfer",
    "pb.SpiTransferResponse": "pb.SpiTransferResponse",
    "pb.SpiConfig": "pb.SpiConfig",
}


def refactor_file(path: Path):
    try:
        with open(path, "r") as f:
            text = f.read()
    except UnicodeDecodeError:
        return

    original_text = text

    # Check if this file has any of the classes
    has_target = any(cls in text for cls in MAPPING.keys())
    if not has_target:
        return

    # Delete imports
    for old_class in MAPPING.keys():
        text = re.sub(rf"^\s*{old_class},?\s*\n", "", text, flags=re.MULTILINE)
        text = re.sub(rf"from mcubridge\.protocol\.structures import {old_class}\s*\n", "", text)

    text = re.sub(r"from mcubridge\.protocol\.structures import \(\s*\)", "", text, flags=re.MULTILINE)
    text = re.sub(r"from \.\.protocol\.structures import \(\s*\)", "", text, flags=re.MULTILINE)

    # Safely replace XPacket instances
    for old_class, new_class in MAPPING.items():
        if old_class in text:
            # decode -> FromString
            text = re.sub(rf"{old_class}\.decode", rf"{new_class}.FromString", text)

            # encode -> SerializeToString
            # To do this safely, we find old_class(...) and then the trailing .encode()
            # But regex is tricky for balanced parenthesis.
            # Instead, we just replace `old_class` with `new_class` first
            text = re.sub(rf"\b{old_class}\b", new_class, text)

    # Now we have things like pb.VersionResponse(...).SerializeToString()
    # Or pb.VersionResponse.SerializeToString()
    # Or just variable = pb.VersionResponse(...); variable.SerializeToString()
    # Actually, in the test files, they chain it: pb.VersionResponse(major=1).SerializeToString()
    # Let's find all instances of `.SerializeToString()` in the file. If the line contains `pb.`, we replace it.
    # WAIT! pb.ProcessRunAsync(command=shlex.join(parts).SerializeToString()).SerializeToString()
    # If we replace ALL .SerializeToString() in a line with `pb.`, we break `shlex.join().SerializeToString()`.
    # Let's use a very safe replacement:
    for cls in MAPPING.values():
        # Match EXACTLY: cls( ... ).encode() without greedy matching
        # Wait, Python regex has no balanced parens.
        # Let's just do it manually by reading the code character by character for `.encode()` after `cls`!
        pass

    # A better approach:
    # Just replace `).encode()` with `).SerializeToString()` ONLY when we are sure it's the end of a protobuf object.
    # Actually, in our codebase, how many `.encode()` are there on these objects?
    # I can just replace `encode()` with `SerializeToString()` where it's clearly chained from the class.
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "pb." in line and ".SerializeToString()" in line:
            # Check if there are multiple .SerializeToString()
            if line.count(".SerializeToString()") == 1:
                # If there's only one, and pb. is in the line, it's very likely the protobuf's encode!
                # EXCEPT: `pb.FileWrite(data=b"").SerializeToString()` - here it's 1 encode.
                # `pb.ProcessRunAsync(command="a".SerializeToString()).SerializeToString()` - 2 encodes.
                # We can just replace the LAST `.SerializeToString()` on the line if it has `pb.`
                lines[i] = re.sub(r"\.encode\(\)(?=[^\.encode\(\)]*$)", ".SerializeToString()", line)
            else:
                # Replace the last one
                idx = line.rfind(".encode()")
                if idx != -1:
                    lines[i] = line[:idx] + ".SerializeToString()" + line[idx + len(".encode()") :]

        elif ".encode()" in line:
            # Check if it's a multiline statement that started with pb.
            # Look backwards up to 5 lines
            for j in range(max(0, i - 5), i):
                if "pb." in lines[j] and "(" in lines[j]:
                    lines[i] = line.replace(".SerializeToString()", ".SerializeToString()")
                    break

    text = "\n".join(lines)

    # Handle imports of pb
    if "mcubridge_pb2" not in text:
        if "mcubridge_client" in str(path):
            if "from __future__" in text:
                text = text.replace(
                    "from __future__ import annotations\n",
                    "from __future__ import annotations\nfrom .protocol import mcubridge_pb2 as pb\n",
                )
            else:
                text = "from .protocol import mcubridge_pb2 as pb\n" + text
        else:
            if "from __future__" in text:
                text = text.replace(
                    "from __future__ import annotations\n",
                    "from __future__ import annotations\nfrom mcubridge.protocol import mcubridge_pb2 as pb\n",
                )
            else:
                text = "from mcubridge.protocol import mcubridge_pb2 as pb\n" + text

    if path.name == "runtime.py":
        old_handler = """    def _gen_handler(self, packet_type: type[Any], callback: Callable[[Any], Awaitable[Any]]) -> McuHandler:
        async def _handler(seq: int, payload: bytes) -> bool | None:
            try:
                p = packet_type.decode(payload)
                res = await callback(p)"""
        new_handler = """    def _gen_handler(self, packet_type: type[Any], callback: Callable[[Any], Awaitable[Any]]) -> McuHandler:
        async def _handler(seq: int, payload: bytes) -> bool | None:
            try:
                p = packet_type()
                p.ParseFromString(payload)
                res = await callback(p)"""
        text = text.replace(old_handler, new_handler)

    if text != original_text:
        with open(path, "w") as f:
            f.write(text)


for root, _, set_files in os.walk("."):
    if ".tox" in root or ".git" in root or ".venv" in root or "__pycache__" in root:
        continue
    for f in set_files:
        if f.endswith(".py"):
            refactor_file(Path(root) / f)

print("Refactor complete.")
