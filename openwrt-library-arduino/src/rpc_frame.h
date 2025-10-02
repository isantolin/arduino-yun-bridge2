#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>

namespace rpc {

constexpr uint8_t START_BYTE = 0x7E;
constexpr uint8_t PROTOCOL_VERSION = 0x02;
constexpr size_t MAX_PAYLOAD_SIZE = 256;
constexpr size_t MAX_FRAME_SIZE = 1 + 5 + MAX_PAYLOAD_SIZE + 2; // Start(1) + Header(5) + Payload(256) + CRC(2)

struct FrameHeader {
    uint8_t version;
    uint16_t payload_length;
    uint16_t command_id;
} __attribute__((packed));

struct Frame {
    FrameHeader header;
    uint8_t payload[MAX_PAYLOAD_SIZE];
};

class FrameParser {
public:
    FrameParser();
    bool consume(uint8_t byte, Frame& out_frame);
    void reset();

private:
    enum class State {
        WAIT_FOR_START,
        READ_HEADER,
        READ_PAYLOAD,
        READ_CRC
    };

    State _state;
    uint16_t _bytes_received;
    uint8_t _header_buffer[sizeof(FrameHeader)];
    uint8_t _crc_buffer[sizeof(uint16_t)];
    Frame _current_frame;
};

class FrameBuilder {
public:
    FrameBuilder();
    bool build(Stream& stream, uint16_t command_id, const uint8_t* payload, uint16_t payload_len);
};

} // namespace rpc

#endif // RPC_FRAME_H
