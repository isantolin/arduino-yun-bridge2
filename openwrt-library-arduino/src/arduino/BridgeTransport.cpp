#include "BridgeTransport.h"
#include "../protocol/rpc_protocol.h"

namespace bridge {

BridgeTransport::BridgeTransport(Stream& stream, HardwareSerial* hwSerial)
    : _stream(stream),
      _hardware_serial(hwSerial),
      _parser(),
      _builder(),
      _flow_paused(false),
      _last_cobs_len(0) {
        // Native array initialization
        memset(_raw_frame_buffer, 0, sizeof(_raw_frame_buffer));
        memset(_last_cobs_frame, 0, sizeof(_last_cobs_frame));
      }

void BridgeTransport::begin(unsigned long baudrate) {
    if (_hardware_serial != nullptr) {
        _hardware_serial->begin(baudrate);
    }
    _flow_paused = false;
    _parser.reset();
}

void BridgeTransport::setBaudrate(unsigned long baudrate) {
    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
        _hardware_serial->end();
        _hardware_serial->begin(baudrate);
    }
}

void BridgeTransport::flush() {
    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
    } else {
        _stream.flush();
    }
}

void BridgeTransport::flushRx() {
    while (_stream.available() > 0) {
        _stream.read();
    }
}

bool BridgeTransport::processInput(rpc::Frame& rxFrame) {
    // Flow Control Logic
    int16_t available_bytes = static_cast<int16_t>(_stream.available());
    
    if (!_flow_paused && available_bytes >= BRIDGE_RX_HIGH_WATER_MARK) {
        // Buffer getting full, pause sender
        // [FIX] Use sendControlFrame to avoid clobbering retransmission buffer
        sendControlFrame(rpc::to_underlying(rpc::CommandId::CMD_XOFF));
        _flow_paused = true;
    } else if (_flow_paused && available_bytes <= BRIDGE_RX_LOW_WATER_MARK) {
        // Buffer drained enough, resume sender
        // [FIX] Use sendControlFrame
        sendControlFrame(rpc::to_underlying(rpc::CommandId::CMD_XON));
        _flow_paused = false;
    }

    while (_stream.available()) {
        int16_t byte_read = static_cast<int16_t>(_stream.read());
        if (byte_read >= 0) {
            uint8_t byte = static_cast<uint8_t>(byte_read);
            bool parsed = _parser.consume(byte, rxFrame);
            if (parsed) {
                return true;
            }
            if (_parser.overflowed()) {
                return false; 
            }
        }
    }
    return false;
}

bool BridgeTransport::_writeAll(const uint8_t* buffer, size_t size) {
    if (!buffer || size == 0) {
        return false;
    }

    size_t total = 0;
    while (total < size) {
        size_t written = 0;
        const uint8_t* chunk = buffer + total;
        const size_t remaining = size - total;

        if (_hardware_serial != nullptr) {
            written = _hardware_serial->write(chunk, remaining);
        } else {
            written = _stream.write(chunk, remaining);
        }

        if (written == 0) {
            return false;
        }
        total += written;
    }

    return true;
}

bool BridgeTransport::sendFrame(uint16_t command_id, const uint8_t* payload, size_t length) {
    size_t raw_len = _builder.build(
        _raw_frame_buffer,
        sizeof(_raw_frame_buffer),
        command_id,
        payload,
        length);

    if (raw_len == 0) {
        return false;
    }

    size_t cobs_len = cobs::encode(_raw_frame_buffer, raw_len, _last_cobs_frame);
    _last_cobs_len = cobs_len;

    const uint8_t terminator = rpc::RPC_FRAME_DELIMITER;
    const bool frame_ok = _writeAll(_last_cobs_frame, cobs_len);

    // Even if the frame write partially failed, attempt to write a delimiter.
    // This helps the receiver resynchronize and avoids concatenating fragments.
    const bool term_ok = _writeAll(&terminator, 1);

    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
    } else {
        _stream.flush();
    }

    return frame_ok && term_ok;
}

bool BridgeTransport::sendControlFrame(uint16_t command_id) {
    constexpr size_t kControlRawMax = sizeof(rpc::FrameHeader) + rpc::CRC_TRAILER_SIZE;
    constexpr size_t kControlCobsMax = kControlRawMax + (kControlRawMax / 254) + 2;
    uint8_t raw_buf[kControlRawMax];
    uint8_t cobs_buf[kControlCobsMax];
    rpc::FrameBuilder builder;
    
    size_t raw_len = builder.build(raw_buf, sizeof(raw_buf), command_id, nullptr, 0);
    if (raw_len == 0) return false;
    
    size_t cobs_len = cobs::encode(raw_buf, raw_len, cobs_buf);
    
    const uint8_t terminator = rpc::RPC_FRAME_DELIMITER;
    const bool frame_ok = _writeAll(cobs_buf, cobs_len);
    const bool term_ok = _writeAll(&terminator, 1);

    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
    } else {
        _stream.flush();
    }

    return frame_ok && term_ok;
}

bool BridgeTransport::retransmitLastFrame() {
    if (_last_cobs_len == 0) return false;

    const uint8_t terminator = rpc::RPC_FRAME_DELIMITER;
    const bool frame_ok = _writeAll(_last_cobs_frame, _last_cobs_len);
    const bool term_ok = _writeAll(&terminator, 1);

    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
    } else {
        _stream.flush();
    }

    return frame_ok && term_ok;
}

void BridgeTransport::reset() {
    _parser.reset();
    _flow_paused = false;
}
}
