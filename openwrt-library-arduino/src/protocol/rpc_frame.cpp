/**
 * @file rpc_frame.cpp
 */
#include "../etl_profile.h"
#include "rpc_frame.h"
#include "rpc_protocol.h"
#include "etl/algorithm.h"

// Canonical CRC32 (IEEE 802.3) - bit-reversed
static uint32_t compute_crc32(const uint8_t* data, size_t length) {
    uint32_t crc = 0xFFFFFFFF;
    while (length--) {
        crc ^= *data++;
        for (int j = 0; j < 8; j++) {
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1));
        }
    }
    return crc ^ 0xFFFFFFFF;
}

namespace rpc {

etl::expected<Frame, FrameError> FrameParser::parse(etl::span<const uint8_t> buffer) {
    if (buffer.size() < 9 || buffer.size() > MAX_RAW_FRAME_SIZE) {
        return etl::unexpected<FrameError>(FrameError::MALFORMED);
    }
    
    const size_t crc_start = buffer.size() - 4;
    const uint32_t received_crc = read_u32_be(&buffer[crc_start]);
    const uint32_t calculated_crc = compute_crc32(buffer.data(), crc_start);

    if (received_crc != calculated_crc) return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);

    // [SIL-2] Mandatory Version Validation
    if (buffer[0] != PROTOCOL_VERSION) {
        return etl::unexpected<FrameError>(FrameError::MALFORMED);
    }

    const uint16_t payload_len = read_u16_be(&buffer[1]);

    // [SIL-2] Length Consistency Check
    // Frame = 1 (Ver) + 2 (Len) + 2 (Cmd) + Payload + 4 (CRC) = 9 + Payload
    if (buffer.size() != (static_cast<size_t>(payload_len) + 9)) {
        return etl::unexpected<FrameError>(FrameError::MALFORMED);
    }

    // [SIL-2] Boundary Check
    if (payload_len > MAX_PAYLOAD_SIZE) {
        return etl::unexpected<FrameError>(FrameError::OVERFLOW);
    }

    Frame result;
    result.header.version = buffer[0];
    result.header.payload_length = payload_len;
    result.header.command_id = read_u16_be(&buffer[3]);

    if (payload_len > 0) {
        etl::copy_n(buffer.begin() + 5, payload_len, result.payload.begin());
    }
    result.crc = calculated_crc;
    return result;
}

size_t FrameBuilder::build(etl::span<uint8_t> buffer, uint16_t command_id, etl::span<const uint8_t> payload) {
    if (payload.size() > MAX_PAYLOAD_SIZE) return 0;

    const uint16_t payload_len = static_cast<uint16_t>(payload.size());
    const size_t data_len = 5 + payload_len;
    const size_t total_len = data_len + 4;

    if (total_len > buffer.size()) return 0;

    // Zero out the entire data area to ensure deterministic CRC
    etl::fill_n(buffer.begin(), data_len, 0);

    buffer[0] = PROTOCOL_VERSION;
    buffer[1] = (payload_len >> 8); buffer[2] = (payload_len & 0xFF);
    buffer[3] = (command_id >> 8);  buffer[4] = (command_id & 0xFF);

    if (payload_len > 0) etl::copy_n(payload.begin(), payload_len, buffer.begin() + 5);

    uint32_t crc = compute_crc32(buffer.data(), data_len);
    write_u32_be(&buffer[data_len], crc);

    return total_len;
}

}
