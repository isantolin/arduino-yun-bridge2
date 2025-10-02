#include "rpc_frame.h"
#include "crc.h"
#include <string.h>

namespace rpc {

// --- FrameParser ---

FrameParser::FrameParser() {
    reset();
}

void FrameParser::reset() {
    _state = State::WAIT_FOR_START;
    _bytes_received = 0;
}

bool FrameParser::consume(uint8_t byte, Frame& out_frame) {
    switch (_state) {
        case State::WAIT_FOR_START:
            if (byte == START_BYTE) {
                _state = State::READ_HEADER;
                _bytes_received = 0;
            }
            break;

        case State::READ_HEADER:
            _header_buffer[_bytes_received++] = byte;
            if (_bytes_received >= sizeof(FrameHeader)) {
                // Safely parse the header from the buffer
                _current_frame.header.version = _header_buffer[0];
                _current_frame.header.payload_length = (uint16_t)_header_buffer[1] | ((uint16_t)_header_buffer[2] << 8);
                _current_frame.header.command_id = (uint16_t)_header_buffer[3] | ((uint16_t)_header_buffer[4] << 8);

                if (_current_frame.header.version != PROTOCOL_VERSION || _current_frame.header.payload_length > MAX_PAYLOAD_SIZE) {
                    reset(); // Invalid header, reset and wait for a new frame
                    break;
                }

                _bytes_received = 0;
                if (_current_frame.header.payload_length == 0) {
                    _state = State::READ_CRC;
                } else {
                    _state = State::READ_PAYLOAD;
                }
            }
            break;

        case State::READ_PAYLOAD:
            _current_frame.payload[_bytes_received++] = byte;
            if (_bytes_received >= _current_frame.header.payload_length) {
                _bytes_received = 0;
                _state = State::READ_CRC;
            }
            break;

        case State::READ_CRC:
            _crc_buffer[_bytes_received++] = byte;
            if (_bytes_received >= sizeof(uint16_t)) {
                uint16_t received_crc = (uint16_t)_crc_buffer[0] | ((uint16_t)_crc_buffer[1] << 8);

                // Calculate CRC on header and payload
                uint16_t calculated_crc = crc16_ccitt_init();
                calculated_crc = crc16_ccitt_update(calculated_crc, _header_buffer, sizeof(FrameHeader));
                calculated_crc = crc16_ccitt_update(calculated_crc, _current_frame.payload, _current_frame.header.payload_length);

                bool crc_ok = (calculated_crc == received_crc);

                reset(); // Reset for the next frame regardless of CRC outcome

                if (crc_ok) {
                    out_frame = _current_frame;
                    return true;
                }
            }
            break;
    }
    return false;
}


// --- FrameBuilder ---

FrameBuilder::FrameBuilder() {}

bool FrameBuilder::build(Stream& stream, uint16_t command_id, const uint8_t* payload, uint16_t payload_len) {
    if (payload_len > MAX_PAYLOAD_SIZE) {
        return false;
    }

    stream.write(START_BYTE);

    // --- Header ---
    uint8_t header[sizeof(FrameHeader)];
    header[0] = PROTOCOL_VERSION;
    header[1] = payload_len & 0xFF;
    header[2] = (payload_len >> 8) & 0xFF;
    header[3] = command_id & 0xFF;
    header[4] = (command_id >> 8) & 0xFF;
    stream.write(header, sizeof(FrameHeader));

    // --- Payload ---
    if (payload && payload_len > 0) {
        stream.write(payload, payload_len);
    }

    // --- CRC ---
    uint16_t crc = crc16_ccitt_init();
    crc = crc16_ccitt_update(crc, header, sizeof(FrameHeader));
    if (payload && payload_len > 0) {
        crc = crc16_ccitt_update(crc, payload, payload_len);
    }

    // Write CRC in little-endian format
    stream.write((uint8_t)(crc & 0xFF));
    stream.write((uint8_t)((crc >> 8) & 0xFF));

    return true;
}

} // namespace rpc
