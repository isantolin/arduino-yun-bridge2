import binascii
import struct
import os

def crc32(data):
    return binascii.crc32(data) & 0xFFFFFFFF

# Constants
PROTOCOL_VERSION = 2
CMD_LINK_SYNC = 2
CMD_LINK_SYNC_RESP = 131

# The log said: "Frame parse error CRC mismatch. Expected 77CF6FF0, got AC920BD0"
# The frame was likely CMD_LINK_SYNC (from Daemon) or CMD_LINK_SYNC_RESP (from MCU).
# Wait, the error was in the DAEMON log.
# So the Daemon received a frame from the MCU.
# The MCU sends CMD_LINK_SYNC_RESP.

# CMD_LINK_SYNC_RESP payload:
# 16 bytes nonce (echoed back)
# 16 bytes HMAC tag (if secret is set)
# Total 32 bytes.

# So my previous assumption of 32 bytes payload was correct for RESP.

# Let's try to find a 32-byte payload that gives CRC 77CF6FF0 or AC920BD0.
# This is hard (brute force).

# However, the mismatch proves they are calculating DIFFERENTLY on the SAME data.
# If I can find ANY data where they differ, I can prove the algorithm is different.

# I already proved that `CRC32` library (on x86 stub) matches `binascii.crc32`.
# But maybe on the AVR (Little Endian) vs MIPS (Big Endian) there is a difference?
# The Yun is MIPS (Big Endian). The MCU is AVR (Little Endian).
# The `CRC32` library processes bytes. It should be endian-agnostic if fed a byte array.

# But `rpc_frame.cpp` writes the CRC as Big Endian.
# `write_u32_be(buffer + data_len, crc);`

# If the MCU calculated `AC920BD0` and wrote it as BE: `AC 92 0B D0`.
# The Daemon read it as BE: `AC 92 0B D0` -> `0xAC920BD0`.
# The Daemon calculated `77CF6FF0` on the same data.

# So:
# CRC_MCU(Data) = 0xAC920BD0
# CRC_DAEMON(Data) = 0x77CF6FF0

# This confirms the ALGORITHMS are different.
# Or the DATA is different.

# If the data was corrupted on the wire, the Daemon would calculate CRC on corrupted data.
# But the MCU calculated CRC on original data.
# So CRC_DAEMON(CorruptedData) != CRC_MCU(OriginalData).
# This is normal for a transmission error.

# BUT, if it happens consistently (every handshake), it's likely a systematic error (algorithm or construction).

# If I changed the algorithm to match Python's, and it STILL fails (blinking), then:
# 1. The data is being corrupted on the wire consistently.
# 2. Or the MCU is constructing the frame differently than the Daemon expects (e.g. header fields).

# Let's look at the header construction in `rpc_frame.cpp`.
# Version (1) + PayloadLen (2) + CmdID (2) = 5 bytes.
# Python `CRC_COVERED_HEADER_FORMAT = ">BHH"` (1+2+2=5 bytes).
# Matches.

# Payload:
# MCU copies payload.
# Daemon expects payload.

# If the CRC I implemented matches Python, then:
# CRC_MCU(Data) == CRC_DAEMON(Data).
# If they still mismatch, then Data_MCU != Data_DAEMON.
# i.e. Corruption.

# Is there a baud rate mismatch?
# Both set to 115200.

# Is there a parity/stop bit mismatch?
# Serial defaults to 8N1.

# Is there a hardware issue?
# "CorrectedSmokeTest" worked (LED blinking).
# But that just means the MCU is running.

# Let's try to get the logs again. I need to know if the CRC error is still there.
# If the error changed to "Timeout" or "Malformed", that's a clue.

print("Ready to debug")
