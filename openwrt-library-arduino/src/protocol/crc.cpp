#include "crc.h"
#include "rpc_protocol.h"

// Removed dependency on external CRC32 library to ensure consistency
// and avoid potential library version mismatches.
// Implements standard IEEE 802.3 CRC32 (polynomial rpc::RPC_CRC_POLYNOMIAL reversed).

uint32_t crc32_ieee(const uint8_t* data, size_t len) {
  constexpr uint32_t kCrcInitial = rpc::RPC_CRC_INITIAL;
  constexpr uint32_t kCrcPolynomial = rpc::RPC_CRC_POLYNOMIAL;

  uint32_t crc = kCrcInitial;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int j = 0; j < 8; j++) {
      if (crc & 1) {
        crc = (crc >> 1) ^ kCrcPolynomial;
      } else {
        crc = (crc >> 1);
      }
    }
  }
  return ~crc;
}