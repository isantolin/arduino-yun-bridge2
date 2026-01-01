#ifndef TEST_CONSTANTS_H
#define TEST_CONSTANTS_H

#include <stdint.h>
#include "protocol/rpc_protocol.h"

constexpr uint16_t TEST_CMD_ID = 0x1234;
constexpr uint16_t TEST_WRITE_U16_VALUE = 0xCDEF;
constexpr uint16_t TEST_CMD_ID_CRC_FAILURE = 0x4444;
constexpr uint16_t TEST_CMD_ID_HEADER_VALIDATION = 0x0102;
constexpr uint16_t TEST_CMD_ID_NOISE = 0x55AA;
constexpr uint16_t TEST_CMD_ID_FRAGMENTATION = 0x9988;
constexpr uint32_t TEST_RANDOM_SEED = 0xDEADBEEF;
constexpr uint32_t TEST_CRC32_VECTOR_EXPECTED = 0x55B401A7;
constexpr uint8_t TEST_PAYLOAD_BYTE = rpc::RPC_TEST_PAYLOAD_BYTE;
constexpr uint8_t TEST_MARKER_BYTE = rpc::RPC_TEST_MARKER_BYTE;
constexpr uint8_t TEST_EXIT_CODE = rpc::RPC_TEST_EXIT_CODE;

#endif // TEST_CONSTANTS_H
