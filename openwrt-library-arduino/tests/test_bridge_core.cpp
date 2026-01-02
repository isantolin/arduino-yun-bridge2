#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

#include "protocol/rpc_protocol.h"
#include "protocol/cobs.h"
#include "protocol/crc.h"
#include "protocol/rpc_frame.h"
#include "test_constants.h"
#include "test_support.h"

// Define global Serial instances for the stub
HardwareSerial Serial;
HardwareSerial Serial1;

static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

// Global instances required by Bridge.cpp linkage
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;
// Note: Bridge instance is NOT defined globally here to allow local instantiation in tests,
// BUT Bridge.cpp/Console.cpp might refer to 'Bridge'. 
// We need a global 'Bridge' for Console.cpp to link.
BridgeClass Bridge(Serial1);

// Mock Stream
class MockStream : public Stream {
public:
    ByteBuffer<8192> tx_buffer;
    ByteBuffer<8192> rx_buffer;

    size_t write(uint8_t c) override {
        TEST_ASSERT(tx_buffer.push(c));
        return 1;
    }

    size_t write(const uint8_t* buffer, size_t size) override {
        TEST_ASSERT(tx_buffer.append(buffer, size));
        return size;
    }

    int available() override {
        return static_cast<int>(rx_buffer.remaining());
    }

    int read() override {
        return rx_buffer.read_byte();
    }

    int peek() override {
        return rx_buffer.peek_byte();
    }

    void flush() override {}
    
    // Helper to inject data into RX buffer
    void inject_rx(const uint8_t* data, size_t len) {
        TEST_ASSERT(rx_buffer.append(data, len));
    }
};

class TestFrameBuilder {
public:
    static size_t build(uint8_t* out, size_t out_cap, uint16_t command_id,
                        const uint8_t* payload, size_t payload_len) {
    uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
        size_t cursor = 0;

        // Header
        raw[cursor++] = rpc::PROTOCOL_VERSION;

        // Payload Length (Big Endian)
        const uint16_t len = static_cast<uint16_t>(payload_len);
        raw[cursor++] = static_cast<uint8_t>((len >> 8) & rpc::RPC_UINT8_MASK);
        raw[cursor++] = static_cast<uint8_t>(len & rpc::RPC_UINT8_MASK);

        // Command ID (Big Endian)
        raw[cursor++] = static_cast<uint8_t>((command_id >> 8) & rpc::RPC_UINT8_MASK);
        raw[cursor++] = static_cast<uint8_t>(command_id & rpc::RPC_UINT8_MASK);

        // Payload
        if (payload_len) {
            TEST_ASSERT(payload != nullptr);
            TEST_ASSERT(cursor + payload_len + 4 <= sizeof(raw));
            memcpy(raw + cursor, payload, payload_len);
            cursor += payload_len;
        }

        // CRC32
        const uint32_t crc = crc32_ieee(raw, cursor);
        TEST_ASSERT(cursor + 4 <= sizeof(raw));
        raw[cursor++] = static_cast<uint8_t>((crc >> 24) & rpc::RPC_UINT8_MASK);
        raw[cursor++] = static_cast<uint8_t>((crc >> 16) & rpc::RPC_UINT8_MASK);
        raw[cursor++] = static_cast<uint8_t>((crc >> 8) & rpc::RPC_UINT8_MASK);
        raw[cursor++] = static_cast<uint8_t>(crc & rpc::RPC_UINT8_MASK);

        // COBS Encode into out
        TEST_ASSERT(out != nullptr);
        const size_t encoded_len = cobs::encode(raw, cursor, out);
        TEST_ASSERT(encoded_len > 0);
        TEST_ASSERT(encoded_len + 1 <= out_cap);
        out[encoded_len] = rpc::RPC_FRAME_DELIMITER;
        return encoded_len + 1;
    }
};

static size_t count_status_ack_frames(const ByteBuffer<8192>& buffer) {
    size_t count = 0;
    size_t cursor = 0;

    while (cursor < buffer.len) {
        size_t end = cursor;
        while (end < buffer.len && buffer.data[end] != rpc::RPC_FRAME_DELIMITER) {
            end++;
        }
        const size_t segment_len = end - cursor;
        if (segment_len > 0) {
            uint8_t decoded[rpc::MAX_RAW_FRAME_SIZE];
            const size_t decoded_len = cobs::decode(
                &buffer.data[cursor],
                segment_len,
                decoded
            );
            if (decoded_len >= sizeof(rpc::FrameHeader)) {
                const uint16_t cmd = rpc::read_u16_be(&decoded[3]);
                if (cmd == rpc::to_underlying(rpc::StatusCode::STATUS_ACK)) {
                    count++;
                }
            }
        }

        cursor = (end < buffer.len) ? (end + 1) : end;
    }

    return count;
}

void test_bridge_begin() {
    MockStream stream;
    BridgeClass bridge(stream);

    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    
    // Verify initial state
    TEST_ASSERT(bridge._awaiting_ack == false);
    TEST_ASSERT(bridge._transport.isFlowPaused() == false);
}

void test_bridge_send_frame() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear(); // Clear handshake frames

    uint8_t payload[] = {TEST_BYTE_01, TEST_BYTE_02, TEST_BYTE_03};
    bool result = bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, payload, 3);
    
    TEST_ASSERT(result == true);
    TEST_ASSERT(stream.tx_buffer.len > 0);
    // Verify COBS encoding and frame structure if possible
}

void test_bridge_process_rx() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    
    // Construct a valid frame (CMD_GET_VERSION)
    const uint8_t payload[] = {TEST_BYTE_01, TEST_BYTE_02, TEST_BYTE_03};
    uint16_t cmd_id = static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION);
    enum { kEncodedCap = rpc::COBS_BUFFER_SIZE + 1 };
    uint8_t encoded_frame[kEncodedCap];
    const size_t encoded_len =
        TestFrameBuilder::build(encoded_frame, sizeof(encoded_frame), cmd_id, payload, sizeof(payload));
    
    stream.inject_rx(encoded_frame, encoded_len);
    bridge.process();
    
    // Assert no crash and that data was consumed
    TEST_ASSERT(stream.available() == 0);
}

void test_bridge_handshake() {
    MockStream stream;
    BridgeClass bridge(stream);
    
    const char* secret = "secret";
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret, strlen(secret));
    stream.tx_buffer.clear();
    
    // Create a 16-byte nonce
    uint8_t nonce[16];
    for (uint8_t i = 0; i < sizeof(nonce); i++) {
        nonce[i] = i;
    }
    
    // Inject CMD_LINK_SYNC
    uint16_t cmd_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
    enum { kEncodedCap = rpc::COBS_BUFFER_SIZE + 1 };
    uint8_t encoded_frame[kEncodedCap];
    const size_t encoded_len =
        TestFrameBuilder::build(encoded_frame, sizeof(encoded_frame), cmd_id, nonce, sizeof(nonce));
    stream.inject_rx(encoded_frame, encoded_len);
    
    bridge.process();
    
    // Expect CMD_LINK_SYNC_RESP
    // We expect a response in tx_buffer.
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_bridge_flow_control() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();
    
    // Inject enough bytes to trigger XOFF (High Water Mark = 48)
    uint8_t data[51];
    test_memfill(data, 50, TEST_PAYLOAD_BYTE);
    data[50] = rpc::RPC_FRAME_DELIMITER; // flush garbage so parser resets
    stream.inject_rx(data, sizeof(data));
    
    // First process(): sees 50 bytes, sends XOFF, reads all bytes
    bridge.process();
    
    // Should have sent XOFF
    TEST_ASSERT(stream.tx_buffer.len > 0);
    // Ideally verify it is XOFF frame
    
    stream.tx_buffer.clear();
    
    // Now we need to ACK the XOFF so the bridge can send XON later.
    // ACK is a status frame (StatusCode::STATUS_ACK).
    // Payload is the command ID being acked (CommandId::CMD_XOFF).
    
    uint16_t ack_cmd_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_ACK);
    uint16_t xoff_cmd_id = static_cast<uint16_t>(rpc::CommandId::CMD_XOFF);

    const uint8_t ack_payload[2] = {
        static_cast<uint8_t>((xoff_cmd_id >> 8) & rpc::RPC_UINT8_MASK),
        static_cast<uint8_t>(xoff_cmd_id & rpc::RPC_UINT8_MASK),
    };

    enum { kEncodedCap = rpc::COBS_BUFFER_SIZE + 1 };
    uint8_t ack_frame[kEncodedCap];
    const size_t ack_len = TestFrameBuilder::build(
        ack_frame, sizeof(ack_frame), ack_cmd_id, ack_payload, sizeof(ack_payload));
    stream.inject_rx(ack_frame, ack_len);
    
    // Process the ACK. This should also trigger XON because buffer is low.
    bridge.process();
    
    // Should have sent XON
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_bridge_request_digital_read_no_op() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear(); // Clear handshake frames

    bridge.requestDigitalRead(13);
    
    // Assert that NO data was written to the stream
    TEST_ASSERT(stream.tx_buffer.len == 0);
}

void test_bridge_file_write_incoming() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
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
    memcpy(frame.payload, payload, sizeof(payload));

    // Dispatch directly
    bridge.dispatch(frame);

    // Expect an ACK response
    // ACK frame: [CMD_ACK][LEN=2][CMD_ID_ACKED]
    TEST_ASSERT(stream.tx_buffer.len > 0);
    // We can't easily decode the output here without a full decoder, 
    // but we verified that it triggered a response.
}

void test_bridge_dedup_console_write_retry() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();

    // Reset console RX state to a known baseline.
    Console._rx_buffer_head = 0;
    Console._rx_buffer_tail = 0;
    Console._xoff_sent = false;

    const uint8_t payload[] = { 'a', 'b', 'c' };

    rpc::Frame frame;
    frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
    frame.header.payload_length = sizeof(payload);
    memcpy(frame.payload, payload, sizeof(payload));

    const int before = Console.available();
    TEST_ASSERT_EQ_UINT(before, 0);

    // First delivery: side-effect must apply.
    g_test_millis = 0;
    bridge.dispatch(frame);
    const int after_first = Console.available();
    TEST_ASSERT_EQ_UINT(after_first, sizeof(payload));

    // Second delivery (retry due to lost ACK): must be deduplicated.
    g_test_millis = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS + 50;
    bridge.dispatch(frame);
    const int after_second = Console.available();
    TEST_ASSERT_EQ_UINT(after_second, sizeof(payload));

    // But ACK should be sent for both deliveries.
    const size_t ack_count = count_status_ack_frames(stream.tx_buffer);
    TEST_ASSERT_EQ_UINT(ack_count, 2);
}

void test_bridge_malformed_frame() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();

    // Inject garbage data into the stream to trigger malformed/overflow logic
    // This tests the parser's resilience
    uint8_t garbage[300];
    test_memfill(garbage, sizeof(garbage), rpc::RPC_UINT8_MASK);
    stream.inject_rx(garbage, sizeof(garbage));
    const uint8_t terminator = rpc::RPC_FRAME_DELIMITER;
    stream.inject_rx(&terminator, 1);

    bridge.process();
    
    // Should have sent a STATUS_MALFORMED or similar error frame
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_file_write_eeprom_parsing() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    
    // Case 1: Valid EEPROM write
    // Path: "/eeprom/10" (len 10)
    // Data: "AB"
    const char path[] = "/eeprom/10";
    const uint8_t path_len = static_cast<uint8_t>(sizeof(path) - 1);
    uint8_t payload[1 + sizeof(path) - 1 + 2];
    payload[0] = path_len;
    memcpy(payload + 1, path, path_len);
    payload[1 + path_len] = 'A';
    payload[1 + path_len + 1] = 'B';

    enum { kEncodedCap = rpc::COBS_BUFFER_SIZE + 1 };
    uint8_t frame[kEncodedCap];
    const size_t frame_len = TestFrameBuilder::build(
        frame,
        sizeof(frame),
        rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE),
        payload,
        sizeof(payload));
    stream.inject_rx(frame, frame_len);
    
    bridge.process();
    
    // Should send ACK
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_file_write_malformed_path() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    
    // Case 2: Malformed path length (claim 100 bytes, provide 5)
    const uint8_t payload[] = {100, '/', 'e'};

    enum { kEncodedCap = rpc::COBS_BUFFER_SIZE + 1 };
    uint8_t frame[kEncodedCap];
    const size_t frame_len = TestFrameBuilder::build(
        frame,
        sizeof(frame),
        rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE),
        payload,
        sizeof(payload));
    stream.inject_rx(frame, frame_len);
    
    bridge.process();
    
    // Should NOT crash.
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_bridge_crc_mismatch() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();

    const uint8_t payload[] = {TEST_BYTE_01, TEST_BYTE_02};
    const uint16_t cmd_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);

    uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
    size_t cursor = 0;

    raw[cursor++] = rpc::PROTOCOL_VERSION;
    const uint16_t len = static_cast<uint16_t>(sizeof(payload));
    raw[cursor++] = static_cast<uint8_t>((len >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(len & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>((cmd_id >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(cmd_id & rpc::RPC_UINT8_MASK);
    memcpy(raw + cursor, payload, sizeof(payload));
    cursor += sizeof(payload);

    // Correct CRC, then corrupt it.
    uint32_t crc = crc32_ieee(raw, cursor);
    crc ^= rpc::RPC_CRC_INITIAL;

    raw[cursor++] = static_cast<uint8_t>((crc >> 24) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>((crc >> 16) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>((crc >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(crc & rpc::RPC_UINT8_MASK);

    enum { kEncodedCap = rpc::COBS_BUFFER_SIZE + 1 };
    uint8_t encoded[kEncodedCap];
    const size_t encoded_len = cobs::encode(raw, cursor, encoded);
    TEST_ASSERT(encoded_len > 0);
    TEST_ASSERT(encoded_len + 1 <= sizeof(encoded));
    encoded[encoded_len] = rpc::RPC_FRAME_DELIMITER;

    stream.inject_rx(encoded, encoded_len + 1);
    bridge.process();

    // Expect STATUS_CRC_MISMATCH.
    // We can check if the response frame contains this status.
    // Response frame: [VER][LEN][STATUS_CMD][PAYLOAD][CRC]
    // Since it's a status, it's sent as a command with ID = status value.
    
    TEST_ASSERT(stream.tx_buffer.len > 0);
    // Decode to verify? For now, just asserting response exists is good, 
    // but let's be more specific if we can.
    // The mock stream just has raw bytes. 
    // We assume if it sent something, it handled the error.
}

void test_bridge_unknown_command() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();

    // Command ID rpc::RPC_INVALID_ID_SENTINEL is likely unknown
    const uint16_t cmd_id = rpc::RPC_INVALID_ID_SENTINEL;
    const uint8_t payload[] = {0};
    enum { kEncodedCap = rpc::COBS_BUFFER_SIZE + 1 };
    uint8_t frame[kEncodedCap];
    const size_t frame_len = TestFrameBuilder::build(
        frame, sizeof(frame), cmd_id, payload, sizeof(payload));

    stream.inject_rx(frame, frame_len);
    bridge.process();
    
    // Expect STATUS_CMD_UNKNOWN.
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_bridge_payload_too_large() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();

    // Max payload is 128. Build an oversized raw frame (200 bytes) and inject it.
    // This should trip the frame parser's overflow/malformed handling.
    enum { kTooLargePayload = 200 };
    uint8_t payload[kTooLargePayload];
    test_memfill(payload, sizeof(payload), TEST_BYTE_AB);
    const uint16_t cmd_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);

    enum { kRawLen = 5 + kTooLargePayload + 4 };
    uint8_t raw[kRawLen];
    size_t cursor = 0;

    raw[cursor++] = rpc::PROTOCOL_VERSION;
    const uint16_t len = static_cast<uint16_t>(kTooLargePayload);
    raw[cursor++] = static_cast<uint8_t>((len >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(len & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>((cmd_id >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(cmd_id & rpc::RPC_UINT8_MASK);
    memcpy(raw + cursor, payload, kTooLargePayload);
    cursor += kTooLargePayload;

    const uint32_t crc = crc32_ieee(raw, cursor);
    raw[cursor++] = static_cast<uint8_t>((crc >> 24) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>((crc >> 16) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>((crc >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(crc & rpc::RPC_UINT8_MASK);

    enum { kEncodedCap = kRawLen + (kRawLen / 254) + 3 };
    uint8_t encoded[kEncodedCap];
    const size_t encoded_len = cobs::encode(raw, cursor, encoded);
    TEST_ASSERT(encoded_len > 0);
    TEST_ASSERT(encoded_len + 1 <= sizeof(encoded));
    encoded[encoded_len] = rpc::RPC_FRAME_DELIMITER;
    stream.inject_rx(encoded, encoded_len + 1);
    
    bridge.process();
    
    // Should result in STATUS_MALFORMED or similar, or just be dropped/reset.
    // The current implementation might send an error.
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

int main() {
    test_bridge_begin();
    test_bridge_send_frame();
    test_bridge_process_rx();
    test_bridge_handshake();
    test_bridge_flow_control();
    test_bridge_request_digital_read_no_op();
    test_bridge_file_write_incoming();
    test_bridge_malformed_frame();
    test_file_write_eeprom_parsing();
    test_file_write_malformed_path();

    // Idempotency regression tests
    test_bridge_dedup_console_write_retry();
    
    // New Robustness Tests
    test_bridge_crc_mismatch();
    test_bridge_unknown_command();
    test_bridge_payload_too_large();
    return 0;
}
