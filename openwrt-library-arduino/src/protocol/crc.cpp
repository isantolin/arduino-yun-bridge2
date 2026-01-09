/**
 * @file crc.cpp
 * @brief CRC32 implementation for frame integrity verification.
 * 
 * [SIL-2 COMPLIANCE]
 * This module provides a self-contained CRC32 implementation to ensure:
 * - Bit-identical results between MCU (C++) and daemon (Python)
 * - No external library dependencies that could drift
 * - Deterministic execution (no heap, no recursion)
 * 
 * Algorithm: IEEE 802.3 CRC32 (same as Ethernet, PNG, ZIP)
 * Polynomial: 0xEDB88320 (bit-reversed representation)
 * Initial value: 0xFFFFFFFF
 * Final XOR: 0xFFFFFFFF (via bitwise NOT)
 * 
 * @param data Pointer to data buffer
 * @param len  Length of data in bytes
 * @return CRC32 checksum (32-bit unsigned)
 */
#include "crc.h"
#include "rpc_protocol.h"

// Removed dependency on external CRC32 library to ensure consistency
// and avoid potential library version mismatches.
// Implements standard IEEE 802.3 CRC32 (polynomial rpc::RPC_CRC_POLYNOMIAL reversed).

uint32_t crc32_ieee(const uint8_t* data, size_t len) {
  uint32_t crc = rpc::RPC_CRC_INITIAL;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t j = 0; j < 8; j++) {
      if (crc & 1) {
        crc = (crc >> 1) ^ rpc::RPC_CRC_POLYNOMIAL;
      } else {
        crc = (crc >> 1);
      }
    }
  }
  return ~crc;
}