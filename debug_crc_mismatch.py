import binascii


def crc32(data):
    return binascii.crc32(data) & 0xFFFFFFFF


# Logged Raw Data: 9000ff020202029010ff0202020202070600ec75
raw_hex = "9000ff020202029010ff0202020202070600ec75"
raw_bytes = binascii.unhexlify(raw_hex)
print(f"Raw Bytes ({len(raw_bytes)}): {raw_bytes.hex()}")

# Last 4 bytes are CRC: 06 00 ec 75.
data_to_check = raw_bytes[:-4]
calc_crc = crc32(data_to_check)
print(f"Calculated CRC: {calc_crc:08X}")

# Expected CRC from log: AFD812CC.
print("Expected CRC: AFD812CC")

if calc_crc == 0xAFD812CC:
    print("MATCH! The Daemon calculated CRC correctly on the garbage data.")
else:
    print("MISMATCH! The Daemon calculated something else?")
