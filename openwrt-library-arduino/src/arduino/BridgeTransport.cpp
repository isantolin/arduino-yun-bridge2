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
        _raw_frame_buffer.fill(0);
        _last_cobs_frame.fill(0);
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

bool BridgeTransport::processInput(rpc::Frame& rxFrame) {
    // Flow Control Logic
    int available_bytes = _stream.available();
    
    if (!_flow_paused && available_bytes >= kRxHighWaterMark) {
        // Buffer getting full, pause sender
        sendFrame(rpc::to_underlying(rpc::CommandId::CMD_XOFF), nullptr, 0);
        _flow_paused = true;
    } else if (_flow_paused && available_bytes <= kRxLowWaterMark) {
        // Buffer drained enough, resume sender
        sendFrame(rpc::to_underlying(rpc::CommandId::CMD_XON), nullptr, 0);
        _flow_paused = false;
    }

    while (_stream.available()) {
        int byte_read = _stream.read();
        if (byte_read >= 0) {
            uint8_t byte = static_cast<uint8_t>(byte_read);
            bool parsed = _parser.consume(byte, rxFrame);
            if (parsed) {
                return true;
            }
            // If overflowed, the caller can check hasOverflowed()
            if (_parser.overflowed()) {
                // We don't return true here, but the caller might want to know about the error
                // For now, we just let the parser state reflect it.
                // The original code emitted a status immediately.
                // We might need to handle that in the caller.
                return false; 
            }
        }
    }
    return false;
}

bool BridgeTransport::sendFrame(uint16_t command_id, const uint8_t* payload, size_t length) {
    size_t raw_len = _builder.build(
        _raw_frame_buffer.data(),
        _raw_frame_buffer.size(),
        command_id,
        payload,
        length);

    if (raw_len == 0) {
        return false;
    }

    size_t cobs_len = cobs::encode(_raw_frame_buffer.data(), raw_len, _last_cobs_frame.data());
    _last_cobs_len = cobs_len;

    // Write COBS frame
    size_t written = 0;
    if (_hardware_serial != nullptr) {
        written = _hardware_serial->write(_last_cobs_frame.data(), cobs_len);
    } else {
        written = _stream.write(_last_cobs_frame.data(), cobs_len);
    }

    if (written != cobs_len) {
        return false;
    }

    // Write terminator
    const uint8_t terminator = 0x00;
    if (_hardware_serial != nullptr) {
        written += _hardware_serial->write(&terminator, 1);
        _hardware_serial->flush(); // Force physical transmission
    } else {
        written += _stream.write(&terminator, 1);
        _stream.flush();
    }

    return written == (cobs_len + 1);
}

bool BridgeTransport::sendControlFrame(uint16_t command_id) {
    uint8_t raw_buf[32];
    uint8_t cobs_buf[34];
    rpc::FrameBuilder builder;
    
    size_t raw_len = builder.build(raw_buf, sizeof(raw_buf), command_id, nullptr, 0);
    if (raw_len == 0) return false;
    
    size_t cobs_len = cobs::encode(raw_buf, raw_len, cobs_buf);
    
    size_t written = 0;
    if (_hardware_serial != nullptr) {
        written = _hardware_serial->write(cobs_buf, cobs_len);
    } else {
        written = _stream.write(cobs_buf, cobs_len);
    }
    
    if (written != cobs_len) return false;
    
    const uint8_t terminator = 0x00;
    if (_hardware_serial != nullptr) {
        written += _hardware_serial->write(&terminator, 1);
    } else {
        written += _stream.write(&terminator, 1);
    }
    
    return written == (cobs_len + 1);
}

bool BridgeTransport::retransmitLastFrame() {
    if (_last_cobs_len == 0) return false;
    
    size_t written = 0;
    if (_hardware_serial != nullptr) {
        written = _hardware_serial->write(_last_cobs_frame.data(), _last_cobs_len);
    } else {
        written = _stream.write(_last_cobs_frame.data(), _last_cobs_len);
    }
    
    if (written != _last_cobs_len) return false;
    
    const uint8_t terminator = 0x00;
    if (_hardware_serial != nullptr) {
        written += _hardware_serial->write(&terminator, 1);
        _hardware_serial->flush();
    } else {
        written += _stream.write(&terminator, 1);
        _stream.flush();
    }
    
    return written == (_last_cobs_len + 1);
}

void BridgeTransport::pauseFlow() {
    if (!_flow_paused) {
        sendControlFrame(rpc::to_underlying(rpc::CommandId::CMD_XOFF));
        _flow_paused = true;
    }
}

void BridgeTransport::resumeFlow() {
    if (_flow_paused) {
        sendControlFrame(rpc::to_underlying(rpc::CommandId::CMD_XON));
        _flow_paused = false;
    }
}
void BridgeTransport::reset() {
    _parser.reset();
    _flow_paused = false;
}
} // namespace bridge
