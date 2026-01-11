#ifndef BRIDGE_TRANSPORT_H
#define BRIDGE_TRANSPORT_H

#include <Arduino.h>
#include <Stream.h>

#include "../bridge_config.h"
#include "protocol/rpc_frame.h"
#include "protocol/cobs.h"

namespace bridge {

// Forward declaration for test interface
namespace test { class TestAccessor; }

class BridgeTransport {
    // Allow test accessor to inspect internal state for unit testing.
    // This replaces the problematic `#define private public` anti-pattern.
    friend class test::TestAccessor;
public:
    explicit BridgeTransport(Stream& stream, HardwareSerial* hwSerial = nullptr);
    
    void begin(unsigned long baudrate);
    void setBaudrate(unsigned long baudrate);
    void flush();     // Flushes TX
    void flushRx();   // [NEW] Flushes RX (drops input buffer)
    
    // Returns true if a frame was parsed and is ready in rxFrame
    bool processInput(rpc::Frame& rxFrame);
    
    // Sends a frame. Returns true on success.
    bool sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
    
    // Retransmits the last frame sent via sendFrame()
    bool retransmitLastFrame();
    
    // Flow control (automatic in processInput)
    bool isFlowPaused() const { return _flow_paused; }
    
    // Stats/Debug
    bool hasOverflowed() const { return _parser.overflowed(); }
    void clearOverflow() { _parser.reset(); }
    rpc::FrameParser::Error getLastError() const { return _parser.getError(); }
    void clearError() { _parser.clearError(); }
    
    // Resets internal state (parser, flow control)
    void reset();

 private:
    // Sends a control frame (no payload) without overwriting the main buffer
    // [SIL-2] Internal use only for flow control (XON/XOFF)
    bool sendControlFrame(uint16_t command_id);

    // Best-effort write-all helper: tries to write the full buffer.
    // Returns true only if all bytes were written.
    bool _writeAll(const uint8_t* buffer, size_t size);

    Stream& _stream;
    HardwareSerial* _hardware_serial;
    rpc::FrameParser _parser;
    rpc::FrameBuilder _builder;
    bool _flow_paused;
    
    // Native C Arrays instead of bridge::array
    uint8_t _raw_frame_buffer[rpc::MAX_RAW_FRAME_SIZE];
    uint8_t _last_cobs_frame[rpc::COBS_BUFFER_SIZE];
    size_t _last_cobs_len;
    
};

} // namespace bridge

#endif // BRIDGE_TRANSPORT_H
