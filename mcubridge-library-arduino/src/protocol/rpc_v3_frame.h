#ifndef RPC_V3_FRAME_H
#define RPC_V3_FRAME_H

#include <stdint.h>
#include <stddef.h>

#include "etl/span.h"

namespace rpc {
namespace v3 {

// V3 Frame Type flags
constexpr uint8_t TYPE_DATAGRAM = 0;
constexpr uint8_t TYPE_RELIABLE = 1;

// V3 Endpoints (Channels)
constexpr uint8_t EP_SYS  = 0;
constexpr uint8_t EP_CTRL = 1;
constexpr uint8_t EP_DATA = 2;
constexpr uint8_t EP_BULK = 3;

// V3 Header Bit-packing definition
#pragma pack(push, 1)
struct Header {
    uint8_t sequence   : 4; // 0-15
    uint8_t endpoint   : 2; // EP_*
    uint8_t compressed : 1; // 1 = compressed
    uint8_t type       : 1; // TYPE_*
};
#pragma pack(pop)

static_assert(sizeof(Header) == 1, "V3 Header must be exactly 1 byte");

// VarInt Decoder for Payload Length
inline size_t decode_varint(const uint8_t* data, size_t max_len, size_t& out_bytes_read) {
    if (max_len == 0) {
        out_bytes_read = 0;
        return 0;
    }
    
    // Fast path: < 128 bytes (1 byte length)
    if ((data[0] & 0x80) == 0) {
        out_bytes_read = 1;
        return data[0];
    }
    
    // Slow path: >= 128 bytes (LEB128 up to 2 bytes for typical MCU payloads)
    if (max_len < 2) {
        out_bytes_read = 0;
        return 0; // Malformed
    }
    
    size_t length = (data[0] & 0x7F) | ((data[1] & 0x7F) << 7);
    out_bytes_read = 2;
    return length;
}

// In-place COBS Decoder
inline size_t cobs_decode_in_place(etl::span<uint8_t> buffer) {
    if (buffer.empty()) return 0;
    
    size_t read_index = 0;
    size_t write_index = 0;
    uint8_t code;
    uint8_t i;
    const size_t length = buffer.size();

    while (read_index < length) {
        code = buffer[read_index];
        
        if (read_index + code > length && code != 1) {
            return 0; // Malformed
        }
        
        read_index++;
        
        for (i = 1; i < code; i++) {
            buffer[write_index++] = buffer[read_index++];
        }
        
        if (code != 0xFF && read_index != length) {
            buffer[write_index++] = '\0';
        }
    }
    
    return write_index;
}

// Fletcher-16 Checksum
inline uint16_t fletcher16(etl::span<const uint8_t> data) {
    uint16_t sum1 = 0;
    uint16_t sum2 = 0;
    for (size_t i = 0; i < data.size(); ++i) {
        sum1 = (sum1 + data[i]) % 255;
        sum2 = (sum2 + sum1) % 255;
    }
    return (sum2 << 8) | sum1;
}

} // namespace v3
} // namespace rpc

#endif // RPC_V3_FRAME_H