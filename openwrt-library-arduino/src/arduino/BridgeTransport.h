#ifndef BRIDGE_TRANSPORT_H
#define BRIDGE_TRANSPORT_H

#include <Arduino.h>
#include <Stream.h>
#include "../bridge_array.h"
#include "protocol/rpc_frame.h"
#include "protocol/cobs.h"

namespace bridge {

class BridgeTransport {
public:
    explicit BridgeTransport(Stream& stream, HardwareSerial* hwSerial = nullptr);
    
    void begin(unsigned long baudrate);
    void flush();
    
    // Returns true if a frame was parsed and is ready in rxFrame
    bool processInput(rpc::Frame& rxFrame);
    
    // Sends a frame. Returns true on success.
    bool sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
    
    // Sends a control frame (no payload) without overwriting the main buffer
    bool sendControlFrame(uint16_t command_id);
    
    // Retransmits the last frame sent via sendFrame()
    bool retransmitLastFrame();
    
    // Flow control
    void pauseFlow();
    void resumeFlow();
    bool isFlowPaused() const { return _flow_paused; }
    
    // Stats/Debug
    bool hasOverflowed() const { return _parser.overflowed(); }
    void clearOverflow() { _parser.reset(); }
    rpc::FrameParser::Error getLastError() const { return _parser.getError(); }
    void clearError() { _parser.clearError(); }
    
    // Resets internal state (parser, flow control)
    void reset();

#if defined(BRIDGE_HOST_TEST)
 public:
#else
 private:
#endif
    Stream& _stream;
    HardwareSerial* _hardware_serial;
    rpc::FrameParser _parser;
    rpc::FrameBuilder _builder;
    bool _flow_paused;
    
    bridge::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> _raw_frame_buffer;
    bridge::array<uint8_t, rpc::COBS_BUFFER_SIZE> _last_cobs_frame;
    size_t _last_cobs_len;
    
    // Constants for flow control
    static constexpr int kRxHighWaterMark = 48;
    static constexpr int kRxLowWaterMark = 16;
};

} // namespace bridge

#endif // BRIDGE_TRANSPORT_H