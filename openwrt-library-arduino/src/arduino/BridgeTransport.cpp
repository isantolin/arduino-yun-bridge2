#include "BridgeTransport.h"
#include "../protocol/rpc_protocol.h"

/**
 * @file BridgeTransport.cpp
 * @brief Serial transport layer for Arduino-Linux RPC communication.
 * * [SIL-2 COMPLIANCE NOTES]
 * - No dynamic memory allocation after initialization
 * - All buffers are statically sized at compile time
 * - Defensive programming with explicit range checks
 * - Flow control (XON/XOFF) prevents buffer overflow
 * - Output Guards: Prevents transmission of invalid/reserved Command IDs
 * * The transport layer handles:
 * - COBS encoding/decoding of frames
 * - CRC verification via rpc::FrameBuilder/FrameParser
 * - Hardware flow control signaling
 * - Frame retransmission for reliability
 */

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
    // [SIL-2] Flow Control Logic with defensive range validation
    // Stream::available() returns int, which could be negative on error.
    // We clamp to valid range [0, INT16_MAX] for safe comparison.
    int raw_available = _stream.available();
    int16_t available_bytes;
    if (raw_available < 0) {
        // Error condition from stream - treat as empty
        available_bytes = 0;
    } else if (raw_available > INT16_MAX) {
        // Saturate to max to prevent overflow
        available_bytes = INT16_MAX;
    } else {
        available_bytes = static_cast<int16_t>(raw_available);
    }
    
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

/**
 * @brief Best-effort write-all helper with retry loop.
 * * [SIL-2] Attempts to write the entire buffer to the stream.
 * Returns true only if ALL bytes were successfully written.
 * Does not block indefinitely - returns false if write returns 0.
 * * @param buffer Pointer to data to write (must not be null)
 * @param size   Number of bytes to write (must be > 0)
 * @return true if all bytes written, false otherwise
 */
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

/**
 * @brief Build and send a complete RPC frame.
 * * [SIL-2] This function:
 * 1. Builds the raw frame (header + payload + CRC) into _raw_frame_buffer
 * 2. COBS-encodes into _last_cobs_frame (retained for retransmission)
 * 3. Writes the encoded frame + delimiter to the stream
 * 4. Flushes to ensure physical transmission
 * * @param command_id RPC command or status code
 * @param payload    Pointer to payload data (may be null if length=0)
 * @param length     Payload length in bytes (max MAX_PAYLOAD_SIZE)
 * @return true if frame was fully transmitted, false on error
 */
bool BridgeTransport::sendFrame(uint16_t command_id, const uint8_t* payload, size_t length) {
    // [SIL-2] GUARD CLAUSE: Strict Protocol Compliance
    // Prevent transmission of invalid Command IDs (0-47 are reserved/invalid).
    // This is a CRITICAL FIX for the "Error Storm" where uninitialized logic
    // might attempt to send Command ID 0, causing the Linux host to reply with
    // an error, triggering an infinite feedback loop.
    if (command_id < rpc::RPC_STATUS_CODE_MIN) {
        return false;
    }

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

/**
 * @brief Send a control frame without payload (e.g., XON/XOFF).
 * * [SIL-2] Unlike sendFrame(), this does NOT overwrite _last_cobs_frame,
 * preserving the ability to retransmit the last data frame if needed.
 * Uses local stack buffers sized for header+CRC only.
 * * @param command_id Control command (typically CMD_XON or CMD_XOFF)
 * @return true if frame was fully transmitted, false on error
 */
bool BridgeTransport::sendControlFrame(uint16_t command_id) {
    // [SIL-2] GUARD CLAUSE: Validate control command ID
    if (command_id < rpc::RPC_STATUS_CODE_MIN) {
        return false;
    }

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

/**
 * @brief Retransmit the last frame sent via sendFrame().
 * * [SIL-2] Used for reliability when ACK timeout occurs.
 * Reuses the pre-encoded COBS data in _last_cobs_frame to avoid
 * re-encoding and ensure bit-identical retransmission.
 * * @return true if retransmission succeeded, false if no frame cached or write failed
 */
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

/**
 * @brief Reset transport state for link re-synchronization.
 * * [SIL-2] Called during link reset to clear:
 * - Parser state (discard any partial frames)
 * - Flow control pause flag
 * * Does NOT clear _last_cobs_frame to allow retransmission if needed.
 */
void BridgeTransport::reset() {
    _parser.reset();
    _flow_paused = false;
}
}
