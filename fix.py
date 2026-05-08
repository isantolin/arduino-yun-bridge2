import re

# We used sed blindly and created SyntaxErrors and TypeError because msgspec Decoder takes (payload, type=...) 
# while our instance MSGPACK_DECODER just takes (payload) ?? Actually msgspec.msgpack.Decoder does NOT take kwargs.
# Correct usage of reusable Decoder is: 
# MSGPACK_DECODER = msgspec.msgpack.Decoder(type=...) -> if we want typed decoding.
# BUT wait! msgspec.msgpack.decode(payload, type=Type) is actually ALREADY C-optimized and uses an internal thread-local buffer!
# Let's check msgspec documentation.
# Actually, the user asked to replace "msgspec.msgpack.decode(payload, type=X)" 
# with "MSGPACK_DECODER.decode(payload)". But Decoder instance MUST be typed at creation, or used without types.
