#include "BridgeTransport.h"

/**
 * @file BridgeTransport.cpp
 * @brief Serial transport layer for Arduino-Linux RPC communication using PacketSerial.
 * * [SIL-2 COMPLIANCE NOTES]
 * - Uses PacketSerial for robust COBS encoding/decoding
 * - Frame construction and validation via rpc::FrameBuilder/FrameParser
 * - No dynamic memory allocation (PacketSerial uses internal buffer or Stream)
 * - CRC32 integrity check enabled
 * - Uses ETL vector for static buffer management
 */

namespace bridge {

// Initialize static instance pointer
BridgeTransport* BridgeTransport::_instance = nullptr;

BridgeTransport::BridgeTransport(Stream& stream, HardwareSerial* hwSerial)
    : _stream(stream),
      _hardware_serial(hwSerial),
      _target_frame(nullptr),
      _frame_received(false),
      _parser()
{
    _instance = this;
    _last_raw_frame.clear();
}

void BridgeTransport::begin(unsigned long baudrate) {
    if (_hardware_serial != nullptr) {
        _hardware_serial->begin(baudrate);
    }
    
    // Configure PacketSerial
    _packetSerial.setStream(&_stream);
    _packetSerial.setPacketHandler(onPacketReceived);
    
    _last_raw_frame.clear();
    _frame_received = false;
    _target_frame = nullptr;
}

void BridgeTransport::end() {
    if (_hardware_serial != nullptr) {
        _hardware_serial->end();
    }
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
    _target_frame = &rxFrame;
    _frame_received = false;
    
    // Process incoming bytes via PacketSerial
    // This will trigger onPacketReceived if a complete frame is decoded
    _packetSerial.update();
    
    _target_frame = nullptr;
    return _frame_received;
}

void BridgeTransport::onPacketReceived(const uint8_t* buffer, size_t size) {
    if (_instance && _instance->_target_frame) {
        // Delegate to FrameParser to validate CRC, Header and fill the frame
        if (_instance->_parser.parse(buffer, size, *_instance->_target_frame)) {
            _instance->_frame_received = true;
        }
        // Parser automatically sets error state if parse fails
    }
}

bool BridgeTransport::sendFrame(uint16_t command_id, const uint8_t* payload, size_t length) {
    // [SIL-2] GUARD CLAUSE: Strict Protocol Compliance
    if (command_id < rpc::RPC_STATUS_CODE_MIN) {
        return false;
    }

    rpc::FrameBuilder builder;
    
    // Ensure we are working with the raw buffer space
    // capacity() returns MAX_RAW_FRAME_SIZE
    size_t raw_len = builder.build(
        _last_raw_frame.data(),
        _last_raw_frame.capacity(),
        command_id,
        payload,
        length);

    if (raw_len == 0) {
        return false;
    }

    // Update size to reflect actual used bytes
    _last_raw_frame.resize(raw_len);

    // Send using PacketSerial (Handles COBS encoding and Delimiter)
    _packetSerial.send(_last_raw_frame.data(), _last_raw_frame.size());

    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
    } else {
        _stream.flush();
    }

    return true;
}

bool BridgeTransport::sendControlFrame(uint16_t command_id) {
    // [SIL-2] GUARD CLAUSE: Validate control command ID
    if (command_id < rpc::RPC_STATUS_CODE_MIN) {
        return false;
    }

    // Build a temporary frame for control messages to avoid overwriting _last_raw_frame
    // (preserving retransmission capability)
    // Using ETL vector here too for consistency, though stack array is fine for temporary
    etl::vector<uint8_t, 32> temp_buf; 
    rpc::FrameBuilder builder;
    
    size_t raw_len = builder.build(temp_buf.data(), temp_buf.capacity(), command_id, nullptr, 0);
    if (raw_len == 0) return false;
    
    temp_buf.resize(raw_len);
    
    _packetSerial.send(temp_buf.data(), temp_buf.size());

    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
    } else {
        _stream.flush();
    }

    return true;
}

bool BridgeTransport::retransmitLastFrame() {
    if (_last_raw_frame.empty()) return false;

    _packetSerial.send(_last_raw_frame.data(), _last_raw_frame.size());

    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
    } else {
        _stream.flush();
    }

    return true;
}

void BridgeTransport::reset() {
    // PacketSerial doesn't have a reset state, but we can clear our flags
    _frame_received = false;
    _target_frame = nullptr;
    // FrameParser state (error) is cleared on next parse
}

rpc::FrameParser::Error BridgeTransport::getLastError() const {
    return _parser.getError();
}

void BridgeTransport::clearError() {
    _parser.clearError();
}

void BridgeTransport::clearOverflow() {
    // PacketSerial doesn't explicitly report overflow in the same way,
    // but FrameParser might set MALFORMED if buffer is too big.
    // We just clear the parser error.
    _parser.clearError();
}

} // namespace bridge