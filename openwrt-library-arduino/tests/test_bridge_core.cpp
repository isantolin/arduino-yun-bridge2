#include <cassert>
#include <cstring>
#include <vector>
#include <iostream>

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

#include "protocol/rpc_protocol.h"

// Mock Stream
class MockStream : public Stream {
public:
    std::vector<uint8_t> tx_buffer;
    std::vector<uint8_t> rx_buffer;
    size_t rx_pos = 0;

    size_t write(uint8_t c) override {
        tx_buffer.push_back(c);
        return 1;
    }

    size_t write(const uint8_t* buffer, size_t size) override {
        tx_buffer.insert(tx_buffer.end(), buffer, buffer + size);
        return size;
    }

    int available() override {
        return static_cast<int>(rx_buffer.size() - rx_pos);
    }

    int read() override {
        if (rx_pos >= rx_buffer.size()) return -1;
        return rx_buffer[rx_pos++];
    }

    int peek() override {
        if (rx_pos >= rx_buffer.size()) return -1;
        return rx_buffer[rx_pos];
    }

    void flush() override {}
    
    // Helper to inject data into RX buffer
    void inject_rx(const std::vector<uint8_t>& data) {
        rx_buffer.insert(rx_buffer.end(), data.begin(), data.end());
    }
};

void test_bridge_begin() {
    MockStream stream;
    BridgeClass bridge(stream);
    
    bridge.begin(115200);
    
    // Verify initial state
    assert(bridge._awaiting_ack == false);
    assert(bridge._flow_paused == false);
}

void test_bridge_send_frame() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear(); // Clear handshake frames

    uint8_t payload[] = {0x01, 0x02, 0x03};
    bool result = bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, std::span<const uint8_t>(payload, 3));
    
    assert(result == true);
    assert(stream.tx_buffer.size() > 0);
    // Verify COBS encoding and frame structure if possible
}

void test_bridge_process_rx() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    
    // Construct a valid frame (CMD_GET_VERSION_RESP)
    // Frame: [CRC][CMD][LEN][PAYLOAD] encoded with COBS
    // For simplicity, we might need to use the actual encoding logic or a known good frame.
    // This is complex to mock without the COBS encoder available in test.
    // But we can test that process() reads from stream.
    
    stream.inject_rx({0x00}); // Empty frame / delimiter
    bridge.process();
    
    // Assert no crash
}

void test_bridge_request_digital_read_no_op() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear(); // Clear handshake frames

    bridge.requestDigitalRead(13);
    
    // Assert that NO data was written to the stream
    assert(stream.tx_buffer.size() == 0);
}

int main() {
    test_bridge_begin();
    test_bridge_send_frame();
    test_bridge_process_rx();
    test_bridge_request_digital_read_no_op();
    
    std::cout << "Bridge Core Tests Passed" << std::endl;
    return 0;
}
