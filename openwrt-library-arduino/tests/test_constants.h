#ifndef TEST_CONSTANTS_H
#define TEST_CONSTANTS_H

#include <stdint.h>
#include "protocol/rpc_protocol.h"

constexpr uint16_t TEST_CMD_ID = 4660;
constexpr uint16_t TEST_WRITE_U16_VALUE = 52719;
constexpr uint16_t TEST_CMD_ID_CRC_FAILURE = 17476;
constexpr uint16_t TEST_CMD_ID_HEADER_VALIDATION = 258;
constexpr uint16_t TEST_CMD_ID_NOISE = 21930;
constexpr uint16_t TEST_CMD_ID_FRAGMENTATION = 39304;
constexpr uint32_t TEST_RANDOM_SEED = 3735928559U;
constexpr uint32_t TEST_CRC32_VECTOR_EXPECTED = 1437860263U;
constexpr uint8_t TEST_PAYLOAD_BYTE = rpc::RPC_TEST_PAYLOAD_BYTE;
constexpr uint8_t TEST_MARKER_BYTE = rpc::RPC_TEST_MARKER_BYTE;
constexpr uint8_t TEST_EXIT_CODE = rpc::RPC_TEST_EXIT_CODE;

constexpr uint8_t TEST_BYTE_00 = 0;
constexpr uint8_t TEST_BYTE_01 = 1;
constexpr uint8_t TEST_BYTE_02 = 2;
constexpr uint8_t TEST_BYTE_03 = 3;
constexpr uint8_t TEST_BYTE_04 = 4;
constexpr uint8_t TEST_BYTE_05 = 5;
constexpr uint8_t TEST_BYTE_06 = 6;
constexpr uint8_t TEST_BYTE_07 = 7;
constexpr uint8_t TEST_BYTE_08 = 8;
constexpr uint8_t TEST_BYTE_09 = 9;
constexpr uint8_t TEST_BYTE_0A = 10;
constexpr uint8_t TEST_BYTE_0B = 11;
constexpr uint8_t TEST_BYTE_0C = 12;
constexpr uint8_t TEST_BYTE_0D = 13;
constexpr uint8_t TEST_BYTE_0E = 14;
constexpr uint8_t TEST_BYTE_0F = 15;
constexpr uint8_t TEST_BYTE_10 = 16;
constexpr uint8_t TEST_BYTE_11 = 17;
constexpr uint8_t TEST_BYTE_12 = 18;
constexpr uint8_t TEST_BYTE_13 = 19;

constexpr uint8_t TEST_BYTE_20 = 32;
constexpr uint8_t TEST_BYTE_22 = 34;
constexpr uint8_t TEST_BYTE_30 = 48;
constexpr uint8_t TEST_BYTE_33 = 51;
constexpr uint8_t TEST_BYTE_34 = 52;
constexpr uint8_t TEST_BYTE_41 = 65;
constexpr uint8_t TEST_BYTE_42 = 66;
constexpr uint8_t TEST_BYTE_44 = 68;
constexpr uint8_t TEST_BYTE_5A = 90;
constexpr uint8_t TEST_BYTE_99 = 153;
constexpr uint8_t TEST_BYTE_AB = 171;
constexpr uint8_t TEST_BYTE_BB = 187;
constexpr uint8_t TEST_BYTE_CC = 204;
constexpr uint8_t TEST_BYTE_DD = 221;
constexpr uint8_t TEST_BYTE_DE = 222;
constexpr uint8_t TEST_BYTE_AD = 173;
constexpr uint8_t TEST_BYTE_BE = 190;
constexpr uint8_t TEST_BYTE_EE = 238;
constexpr uint8_t TEST_BYTE_EF = 239;

#endif // TEST_CONSTANTS_H
