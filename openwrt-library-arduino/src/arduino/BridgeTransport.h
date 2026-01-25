#ifndef BRIDGE_TRANSPORT_H
#define BRIDGE_TRANSPORT_H

#include <Arduino.h>
#include <Stream.h>
#include <PacketSerial.h>
#include <etl/vector.h>
#include "../protocol/rpc_protocol.h"
#include "../protocol/rpc_frame.h" // Needed for rpc::Frame and FrameParser::Error

namespace bridge {

namespace test { class TestAccessor; }

class BridgeTransport {
    friend class test::TestAccessor;
public:
    BridgeTransport(Stream& stream, HardwareSerial* hwSerial = nullptr);
    
    void begin(unsigned long baudrate);
    void end(); // Not strictly used by Bridge.cpp but good practice
    void setBaudrate(unsigned long baudrate);
    void flush();
    void flushRx();

    // Polling loop (replaces processInput logic)
    bool processInput(rpc::Frame& rxFrame);

    // Sending frames
    bool sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
    bool sendControlFrame(uint16_t command_id);
    bool retransmitLastFrame();
    void reset();

    // Error handling accessors for Bridge.cpp
    rpc::FrameParser::Error getLastError() const;
    void clearError();
    void clearOverflow();

    // Internal Callback Trampoline
    static void onPacketReceived(const uint8_t* buffer, size_t size);

private:
    Stream& _stream;
    HardwareSerial* _hardware_serial;
    
    PacketSerial _packetSerial;
    
    // Buffer for retransmission (Raw Frame: Header + Payload + CRC)
    etl::vector<uint8_t, rpc::MAX_RAW_FRAME_SIZE> _last_raw_frame;
    
    // State for processInput polling
    rpc::Frame* _target_frame;
    bool _frame_received;
    
    // Error tracking
    rpc::FrameParser _parser; // Helper for parsing, also tracks error enum
    
    // Global instance pointer for the static callback
    static BridgeTransport* _instance;
    
    bool _writeAll(const uint8_t* buffer, size_t size);
};

} // namespace bridge

#endif // BRIDGE_TRANSPORT_H
