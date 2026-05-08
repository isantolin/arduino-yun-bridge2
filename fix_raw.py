import msgspec
import msgspec.msgpack
from typing import Annotated

# Test exactly how msgspec.Raw works with struct decoding
class DatastorePutPacket(msgspec.Struct, frozen=True, array_like=True):
    key: str
    value: msgspec.Raw

payload = msgspec.msgpack.encode(DatastorePutPacket(key="temp", value=msgspec.Raw(b"25.5")))
print(f"Encoded Hex: {payload.hex()}")
try:
    packet = msgspec.msgpack.decode(payload, type=DatastorePutPacket)
    print(f"Decoded Key: {packet.key}")
    print(f"Decoded Raw Value: {packet.value}")
    print(f"Value as Bytes: {bytes(packet.value)}")
except Exception as e:
    print(f"Decode Error: {e}")
