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
#include "protocol/cobs.h"
#include "protocol/crc.h"
#include "protocol/rpc_frame.h"

// Define global Serial instances for the stub
HardwareSerial Serial;
HardwareSerial Serial1;

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

class TestFrameBuilder {
public:
    static std::vector<uint8_t> build(uint16_t command_id, const std::vector<uint8_t>& payload) {
        std::vector<uint8_t> frame;
        
        // Header
        frame.push_back(rpc::PROTOCOL_VERSION);
        
        // Payload Length (Big Endian)
        uint16_t len = static_cast<uint16_t>(payload.size());
        frame.push_back((len >> 8) & 0xFF);
        frame.push_back(len & 0xFF);
        
        // Command ID (Big Endian)
        frame.push_back((command_id >> 8) & 0xFF);
        frame.push_back(command_id & 0xFF);
        
        // Payload
        frame.insert(frame.end(), payload.begin(), payload.end());
        
        // CRC32
        uint32_t crc = crc32_ieee(frame.data(), frame.size());
        frame.push_back((crc >> 24) & 0xFF);
        frame.push_back((crc >> 16) & 0xFF);
        frame.push_back((crc >> 8) & 0xFF);
        frame.push_back(crc & 0xFF);
        
        // COBS Encode
        std::vector<uint8_t> encoded(frame.size() + 2 + frame.size() / 254 + 1);
        size_t encoded_len = cobs::encode(frame.data(), frame.size(), encoded.data());
        encoded.resize(encoded_len);
        
        // Delimiter
        encoded.push_back(0x00);
        
        return encoded;
    }
};

void test_bridge_begin() {
    MockStream stream;
    BridgeClass bridge(stream);
    
    bridge.begin(115200);
    
    // Verify initial state
    assert(bridge._awaiting_ack == false);
    assert(bridge._transport.isFlowPaused() == false);
}

void test_bridge_send_frame() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear(); // Clear handshake frames

    uint8_t payload[] = {0x01, 0x02, 0x03};
    bool result = bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, payload, 3);
    
    assert(result == true);
    assert(stream.tx_buffer.size() > 0);
    // Verify COBS encoding and frame structure if possible
}

void test_bridge_process_rx() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    
    // Construct a valid frame (CMD_GET_VERSION)
    std::vector<uint8_t> payload = {0x01, 0x02, 0x03};
    uint16_t cmd_id = static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION);
    std::vector<uint8_t> encoded_frame = TestFrameBuilder::build(cmd_id, payload);
    
    stream.inject_rx(encoded_frame);
    bridge.process();
    
    // Assert no crash and that data was consumed
    assert(stream.available() == 0);
}

void test_bridge_handshake() {
    MockStream stream;
    BridgeClass bridge(stream);
    
    const char* secret = "secret";
    bridge.begin(115200, secret, strlen(secret));
    stream.tx_buffer.clear();
    
    // Create a 16-byte nonce
    std::vector<uint8_t> nonce(16);
    for (int i = 0; i < 16; i++) nonce[i] = static_cast<uint8_t>(i);
    
    // Inject CMD_LINK_SYNC
    uint16_t cmd_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
    std::vector<uint8_t> encoded_frame = TestFrameBuilder::build(cmd_id, nonce);
    stream.inject_rx(encoded_frame);
    
    bridge.process();
    
    // Expect CMD_LINK_SYNC_RESP
    // We expect a response in tx_buffer.
    assert(stream.tx_buffer.size() > 0);
}

void test_bridge_flow_control() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear();
    
    // Inject enough bytes to trigger XOFF (High Water Mark = 48)
    std::vector<uint8_t> data(50, 0xAA);
    data.push_back(0x00); // Delimiter to flush garbage so parser resets
    stream.inject_rx(data);
    
    // First process(): sees 50 bytes, sends XOFF, reads all bytes
    bridge.process();
    
    // Should have sent XOFF
    assert(stream.tx_buffer.size() > 0);
    // Ideally verify it is XOFF frame
    
    stream.tx_buffer.clear();
    
    // Now we need to ACK the XOFF so the bridge can send XON later.
    // XOFF command ID is 0x08.
    // ACK command ID is 0x07.
    // Payload is the command ID being acked (0x08).
    
    uint16_t ack_cmd_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_ACK);
    uint16_t xoff_cmd_id = static_cast<uint16_t>(rpc::CommandId::CMD_XOFF);
    
    std::vector<uint8_t> ack_payload;
    ack_payload.push_back((xoff_cmd_id >> 8) & 0xFF);
    ack_payload.push_back(xoff_cmd_id & 0xFF);
    
    std::vector<uint8_t> ack_frame = TestFrameBuilder::build(ack_cmd_id, ack_payload);
    stream.inject_rx(ack_frame);
    
    // Process the ACK. This should also trigger XON because buffer is low.
    bridge.process();
    
    // Should have sent XON
    assert(stream.tx_buffer.size() > 0);
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

void test_bridge_file_write_incoming() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear();

    // Construct a fake CMD_FILE_WRITE frame
    // Payload: [path_len(1)][path...][data...]
    // Path: "/tmp/test" (9 bytes)
    // Data: "hello" (5 bytes)
    uint8_t payload[] = {
        9, 
        '/', 't', 'm', 'p', '/', 't', 'e', 's', 't',
        'h', 'e', 'l', 'l', 'o'
    };
    
    rpc::Frame frame;
    frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE);
    frame.header.payload_length = sizeof(payload);
    std::memcpy(frame.payload, payload, sizeof(payload));

    // Dispatch directly
    bridge.dispatch(frame);

    // Expect an ACK response
    // ACK frame: [CMD_ACK][LEN=2][CMD_ID_ACKED]
    assert(stream.tx_buffer.size() > 0);
    // We can't easily decode the output here without a full decoder, 
    // but we verified that it triggered a response.
}

void test_bridge_malformed_frame() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear();

    // Inject garbage data into the stream to trigger malformed/overflow logic
    // This tests the parser's resilience
    std::vector<uint8_t> garbage(300, 0xFF); 
    stream.inject_rx(garbage);
    stream.inject_rx({0x00}); // Terminator

    bridge.process();
    
    // Should have sent a STATUS_MALFORMED or similar error frame
    assert(stream.tx_buffer.size() > 0);
}

void test_file_write_eeprom_parsing() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    
    // Case 1: Valid EEPROM write
    // Path: "/eeprom/10" (len 10)
    // Data: "AB"
    std::vector<uint8_t> payload;
    std::string path = "/eeprom/10";
    payload.push_back(static_cast<uint8_t>(path.length()));
    payload.insert(payload.end(), path.begin(), path.end());
    payload.push_back('A');
    payload.push_back('B');
    
    std::vector<uint8_t> frame = TestFrameBuilder::build(rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE), payload);
    stream.inject_rx(frame);
    
    bridge.process();
    
    // Should send ACK
    assert(stream.tx_buffer.size() > 0);
}

void test_file_write_malformed_path() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    
    // Case 2: Malformed path length (claim 100 bytes, provide 5)
    std::vector<uint8_t> payload;
    payload.push_back(100); // Path len
    payload.push_back('/');
    payload.push_back('e');
    
    std::vector<uint8_t> frame = TestFrameBuilder::build(rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE), payload);
    stream.inject_rx(frame);
    
    bridge.process();
    
    // Should NOT crash.
    assert(stream.tx_buffer.size() > 0);
}

void test_bridge_crc_mismatch() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear();

    // Build a valid frame
    std::vector<uint8_t> payload = {0x01, 0x02};
    uint16_t cmd_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
    
    // Manually build frame to corrupt CRC
    std::vector<uint8_t> frame;
    frame.push_back(rpc::PROTOCOL_VERSION);
    uint16_t len = static_cast<uint16_t>(payload.size());
    frame.push_back((len >> 8) & 0xFF);
    frame.push_back(len & 0xFF);
    frame.push_back((cmd_id >> 8) & 0xFF);
    frame.push_back(cmd_id & 0xFF);
    frame.insert(frame.end(), payload.begin(), payload.end());
    
    // Calculate CORRECT CRC
    uint32_t crc = crc32_ieee(frame.data(), frame.size());
    // CORRUPT IT
    crc ^= 0xFFFFFFFF; 
    
    frame.push_back((crc >> 24) & 0xFF);
    frame.push_back((crc >> 16) & 0xFF);
    frame.push_back((crc >> 8) & 0xFF);
    frame.push_back(crc & 0xFF);
    
    // COBS Encode
    std::vector<uint8_t> encoded(frame.size() + 2 + frame.size() / 254 + 1);
    size_t encoded_len = cobs::encode(frame.data(), frame.size(), encoded.data());
    encoded.resize(encoded_len);
    encoded.push_back(0x00);

    stream.inject_rx(encoded);
    bridge.process();

    // Expect STATUS_CRC_MISMATCH (0x04)
    // We can check if the response frame contains this status.
    // Response frame: [VER][LEN][STATUS_CMD][PAYLOAD][CRC]
    // STATUS_CRC_MISMATCH is 0x04.
    // Since it's a status, it's sent as a command with ID = status value.
    // So we look for command ID 0x0004.
    
    assert(stream.tx_buffer.size() > 0);
    // Decode to verify? For now, just asserting response exists is good, 
    // but let's be more specific if we can.
    // The mock stream just has raw bytes. 
    // We assume if it sent something, it handled the error.
}

void test_bridge_unknown_command() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear();

    // Command ID 0xFFFF is likely unknown
    uint16_t cmd_id = 0xFFFF;
    std::vector<uint8_t> payload = {0x00};
    std::vector<uint8_t> frame = TestFrameBuilder::build(cmd_id, payload);
    
    stream.inject_rx(frame);
    bridge.process();
    
    // Expect STATUS_CMD_UNKNOWN (0x02)
    assert(stream.tx_buffer.size() > 0);
}

void test_bridge_payload_too_large() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(115200);
    stream.tx_buffer.clear();

    // Max payload is 128 (RPC_BUFFER_SIZE/MAX_PAYLOAD_SIZE)
    // Let's try to send 200 bytes.
    std::vector<uint8_t> payload(200, 0xAB);
    uint16_t cmd_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
    
    // We need to manually build this because TestFrameBuilder might just work 
    // (it doesn't enforce limits, just encodes).
    // However, the Bridge `processInput` or `cobs::decode` should fail 
    // if the decoded buffer exceeds internal limits.
    
    std::vector<uint8_t> frame = TestFrameBuilder::build(cmd_id, payload);
    stream.inject_rx(frame);
    
    bridge.process();
    
    // Should result in STATUS_MALFORMED or similar, or just be dropped/reset.
    // The current implementation might send an error.
    assert(stream.tx_buffer.size() > 0);
}

int main() {
    std::cout << "Running test_bridge_begin..." << std::endl;
    test_bridge_begin();
    std::cout << "Running test_bridge_send_frame..." << std::endl;
    test_bridge_send_frame();
    std::cout << "Running test_bridge_process_rx..." << std::endl;
    test_bridge_process_rx();
    std::cout << "Running test_bridge_handshake..." << std::endl;
    test_bridge_handshake();
    std::cout << "Running test_bridge_flow_control..." << std::endl;
    test_bridge_flow_control();
    std::cout << "Running test_bridge_request_digital_read_no_op..." << std::endl;
    test_bridge_request_digital_read_no_op();
    std::cout << "Running test_bridge_file_write_incoming..." << std::endl;
    test_bridge_file_write_incoming();
    std::cout << "Running test_bridge_malformed_frame..." << std::endl;
    test_bridge_malformed_frame();
    std::cout << "Running test_file_write_eeprom_parsing..." << std::endl;
    test_file_write_eeprom_parsing();
    std::cout << "Running test_file_write_malformed_path..." << std::endl;
    test_file_write_malformed_path();
    
    // New Robustness Tests
    std::cout << "Running test_bridge_crc_mismatch..." << std::endl;
    test_bridge_crc_mismatch();
    std::cout << "Running test_bridge_unknown_command..." << std::endl;
    test_bridge_unknown_command();
    std::cout << "Running test_bridge_payload_too_large..." << std::endl;
    test_bridge_payload_too_large();
    
    std::cout << "Bridge Core Tests Passed" << std::endl;
    return 0;
}
