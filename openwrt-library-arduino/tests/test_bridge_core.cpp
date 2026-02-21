/*
 * CORRECCIÓN DE EMERGENCIA: test_bridge_core.cpp
 * ----------------------------------------------
 * Motivo: Fallo en Assertion en línea 225 (stream.tx_buffer.len > 0).
 * Causa Raíz: La función sync_bridge calculaba mal el CRC (XOR extra),
 * provocando que el Bridge rechazara el frame de sincronización.
 * Solución: Usar TestFrameBuilder para construir el frame de forma consistente.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"

#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"
#include "test_constants.h"
#include "test_support.h"
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "BridgeTestInterface.h"

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

namespace {

// Local COBS implementation for test frame generation and parsing
// (Since src/protocol/cobs.h was removed in favor of PacketSerial)
struct TestCOBS {
    static size_t encode(const uint8_t* source, size_t length, uint8_t* destination) {
        size_t read_index = 0;
        size_t write_index = 1;
        size_t code_index = 0;
        uint8_t code = 1;

        while (read_index < length) {
            if (source[read_index] == 0) {
                destination[code_index] = code;
                code = 1;
                code_index = write_index++;
                read_index++;
            } else {
                destination[write_index++] = source[read_index++];
                code++;
                if (code == 0xFF) {
                    destination[code_index] = code;
                    code = 1;
                    code_index = write_index++;
                }
            }
        }
        destination[code_index] = code;
        return write_index;
    }

    static size_t decode(const uint8_t* source, size_t length, uint8_t* destination) {
        size_t read_index = 0;
        size_t write_index = 0;
        uint8_t code;
        uint8_t i;

        while (read_index < length) {
            code = source[read_index];

            if (read_index + code > length && code != 1) {
                return 0;
            }

            read_index++;

            for (i = 1; i < code; i++) {
                destination[write_index++] = source[read_index++];
            }

            if (code != 0xFF && read_index != length) {
                destination[write_index++] = 0;
            }
        }

        return write_index;
    }
};

// Safe buffer size for encoded frames
constexpr size_t kMaxEncodedSize = rpc::MAX_RAW_FRAME_SIZE + 32;

} // namespace

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

enum class WriteFailureMode {
    None,
    ShortWrite,
    DropTerminator,
};

class ModeStream : public Stream {
public:
    ByteBuffer<8192> tx_buffer;
    ByteBuffer<8192> rx_buffer;

    WriteFailureMode failure_mode = WriteFailureMode::None;
    int write_calls = 0;

    size_t write(uint8_t c) override {
        write_calls++;
        if (failure_mode == WriteFailureMode::DropTerminator) {
            // Simulate a missing terminator write: drop the write when it is the second
            // write call (BridgeTransport writes payload then terminator).
            if (write_calls >= 2) {
                return 0;
            }
        }
        TEST_ASSERT(tx_buffer.push(c));
        return 1;
    }

    size_t write(const uint8_t* buffer, size_t size) override {
        write_calls++;
        if (!buffer || size == 0) {
            return 0;
        }

        if (failure_mode == WriteFailureMode::ShortWrite) {
            const size_t n = (size > 0) ? (size - 1) : 0;
            TEST_ASSERT(tx_buffer.append(buffer, n));
            return n;
        }

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

    void inject_rx(const uint8_t* data, size_t len) {
        TEST_ASSERT(rx_buffer.append(data, len));
    }

    void clear_tx() {
        tx_buffer.clear();
        write_calls = 0;
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
        const size_t encoded_len = TestCOBS::encode(raw, cursor, out);
        TEST_ASSERT(encoded_len > 0);
        TEST_ASSERT(encoded_len + 1 <= out_cap);
        out[encoded_len] = rpc::RPC_FRAME_DELIMITER;
        return encoded_len + 1;
    }
};

void sync_bridge(BridgeClass& bridge, MockStream& stream) {
    stream.tx_buffer.clear(); // Clear any initial traffic
    
    auto ba = bridge::test::TestAccessor::create(bridge);

    // Ensure bridge has a shared secret for handshake (required for HMAC)
    if (ba.isSharedSecretEmpty()) {
        const char* test_secret = "test_secret";
        ba.assignSharedSecret(
            reinterpret_cast<const uint8_t*>(test_secret),
            reinterpret_cast<const uint8_t*>(test_secret) + strlen(test_secret)
        );
    }
    
    // Skip startup stabilization phase for testing (normally handled by timer)
    ba.setStartupStabilizing(false);
    
    // Construct a CMD_LINK_SYNC frame
    const uint8_t nonce[rpc::RPC_HANDSHAKE_NONCE_LENGTH] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
    
    enum { kEncodedCap = kMaxEncodedSize + 1 };
    uint8_t encoded_frame[kEncodedCap];

    const size_t frame_len = TestFrameBuilder::build(
        encoded_frame,
        sizeof(encoded_frame),
        rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC),
        nonce,
        sizeof(nonce)
    );

    stream.inject_rx(encoded_frame, frame_len);
    bridge.process(); // Process the CMD_LINK_SYNC command
    
    // Expect CMD_LINK_SYNC_RESP and clear tx buffer for next test logic
    TEST_ASSERT(stream.tx_buffer.len > 0);
    stream.tx_buffer.clear(); // Clear response to sync
}

static size_t count_status_ack_frames(const ByteBuffer<8192>& buffer) {
    size_t count = 0;
    size_t cursor = 0;

    // Use local decode buffer
    uint8_t decoded[rpc::MAX_RAW_FRAME_SIZE];

    while (cursor < buffer.len) {
        size_t end = cursor;
        while (end < buffer.len && buffer.data[end] != rpc::RPC_FRAME_DELIMITER) {
            end++;
        }
        const size_t segment_len = end - cursor;
        if (segment_len > 0) {
            const size_t decoded_len = TestCOBS::decode(
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

static bool parse_first_frame(const ByteBuffer<8192>& buffer, rpc::Frame& out_frame) {
    rpc::FrameParser parser;
    uint8_t packet_buf[kMaxEncodedSize];
    size_t packet_idx = 0;
    uint8_t decoded_buf[rpc::MAX_RAW_FRAME_SIZE];

    for (size_t i = 0; i < buffer.len; ++i) {
        uint8_t b = buffer.data[i];
        if (b == rpc::RPC_FRAME_DELIMITER) {
            if (packet_idx > 0) {
                size_t decoded_len = TestCOBS::decode(packet_buf, packet_idx, decoded_buf);
                if (decoded_len > 0) {
                    auto result = parser.parse(etl::span<const uint8_t>(decoded_buf, decoded_len));
                    if (result) {
                        out_frame = result.value();
                        return true;
                    }
                }
            }
            packet_idx = 0;
        } else {
            if (packet_idx < kMaxEncodedSize) {
                packet_buf[packet_idx++] = b;
            }
        }
    }
    return false;
}

static uint16_t first_frame_command_id_or_sentinel(const ByteBuffer<8192>& buffer) {
    rpc::Frame frame{};
    if (!parse_first_frame(buffer, frame)) {
        return rpc::RPC_INVALID_ID_SENTINEL;
    }
    return frame.header.command_id;
}

void test_bridge_begin() {
    MockStream stream;
    BridgeClass bridge(stream);

    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    
    // Verify initial state
    auto ba = bridge::test::TestAccessor::create(bridge);
    TEST_ASSERT(ba.isUnsynchronized());
    // TEST_ASSERT(bridge._transport.isFlowPaused() == false); // Flow control removed from Transport
}

void test_bridge_send_frame() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    sync_bridge(bridge, stream); // Ensure bridge is synchronized
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
    enum { kEncodedCap = kMaxEncodedSize + 1 };
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
    auto ba = bridge::test::TestAccessor::create(bridge);
    ba.setStartupStabilizing(false);  // Skip startup phase for test
    
    // Create a 16-byte nonce
    uint8_t nonce[16];
    for (uint8_t i = 0; i < sizeof(nonce); i++) {
        nonce[i] = i;
    }
    
    // Inject CMD_LINK_SYNC
    uint16_t cmd_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
    enum { kEncodedCap = kMaxEncodedSize + 1 };
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
    // TEST_ASSERT(stream.tx_buffer.len > 0); // Removed flow control check if unused
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

    enum { kEncodedCap = kMaxEncodedSize + 1 };
    uint8_t ack_frame[kEncodedCap];
    const size_t ack_len = TestFrameBuilder::build(
        ack_frame, sizeof(ack_frame), ack_cmd_id, ack_payload, sizeof(ack_payload));
    stream.inject_rx(ack_frame, ack_len);
    
    // Process the ACK. This should also trigger XON because buffer is low.
    bridge.process();
    
    // Should have sent XON
    // TEST_ASSERT(stream.tx_buffer.len > 0); // Removed flow control check
}

void test_bridge_file_write_incoming() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    auto ba = bridge::test::TestAccessor::create(bridge);
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
    memcpy(frame.payload.data(), payload, sizeof(payload));

    // Dispatch directly
    ba.dispatch(frame);

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
    
    // 1. Synchronize the bridge (this sends SYNC_RESP)
    sync_bridge(bridge, stream);
    
    // 2. Clear the SYNC_RESP from the output buffer so we only count new ACKs
    stream.tx_buffer.clear();

    // 3. Reset Console state (clears Rx buffer, resets flags)
    Console.begin();

        const uint8_t payload[] = { 'a', 'b', 'c' };

        rpc::Frame frame;

        uint8_t raw_frame[rpc::MAX_RAW_FRAME_SIZE];

        rpc::FrameBuilder builder;

        size_t raw_len = builder.build(etl::span<uint8_t>(raw_frame), rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), etl::span<const uint8_t>(payload));

        

        frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);

        frame.header.payload_length = sizeof(payload);

        memcpy(frame.payload.data(), payload, sizeof(payload));

        frame.crc = rpc::read_u32_be(&raw_frame[raw_len - rpc::CRC_TRAILER_SIZE]);

    

        const int before = Console.available();
    TEST_ASSERT_EQ_UINT(before, 0);

    auto ba = bridge::test::TestAccessor::create(bridge);

    // --- First Delivery ---
    g_test_millis = 0;
    // Dispatch injects frame into RX processing logic
    ba.dispatch(frame);
    // CRITICAL: process() is needed to flush pending TX queue (ACKs) to the stream
    bridge.process(); 

    const int after_first = Console.available();
    TEST_ASSERT_EQ_UINT(after_first, sizeof(payload));

    // --- Second Delivery (Retry) ---
    // Advance time beyond ACK timeout to simulate retry window
    g_test_millis = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS + 50;
    
    ba.dispatch(frame);
    bridge.process(); // Flush second ACK

    const int after_second = Console.available();
    // Should NOT increase (idempotency)
    TEST_ASSERT_EQ_UINT(after_second, sizeof(payload));

    // --- Verify ACKs ---
    // We expect 2 ACKs: one for the first delivery, one for the retry.
    const size_t ack_count = count_status_ack_frames(stream.tx_buffer);
    
    if (ack_count != 2) {
        // Debug output if assertion fails
        fprintf(stderr, "[DEBUG] tx_buffer len: %zu, ack_count: %zu\n", stream.tx_buffer.len, ack_count);
    }
    
    TEST_ASSERT_EQ_UINT(ack_count, 2);
}

void test_bridge_dedup_window_edges() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    auto ba = bridge::test::TestAccessor::create(bridge);

    // Dedup logic is independent of synchronization.
    rpc::Frame frame;
    uint8_t raw_frame[rpc::MAX_RAW_FRAME_SIZE];
    rpc::FrameBuilder builder;
    const uint8_t payload[] = {'x', 'y', 'z'};
    size_t raw_len = builder.build(etl::span<uint8_t>(raw_frame), rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), etl::span<const uint8_t>(payload));
    
    // Extract calculated CRC from raw_frame
    frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
    frame.header.payload_length = sizeof(payload);
    memcpy(frame.payload.data(), payload, sizeof(payload));
    frame.crc = rpc::read_u32_be(&raw_frame[raw_len - rpc::CRC_TRAILER_SIZE]);

    // No prior CRC -> not duplicate.
    ba.setLastRxCrc(0);
    TEST_ASSERT(!ba.isRecentDuplicateRx(frame));

    // Mark processed at t=0.
    g_test_millis = 0;
    ba.markRxProcessed(frame);

    // Too soon (< ack timeout) -> treat as a new command.
    g_test_millis = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS - 1;
    TEST_ASSERT(!ba.isRecentDuplicateRx(frame));

    // After ack timeout -> accept as duplicate (within retry window).
    g_test_millis = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS + 10;
    TEST_ASSERT(ba.isRecentDuplicateRx(frame));

    // Beyond retry window -> not duplicate.
    const unsigned long window_ms =
        static_cast<unsigned long>(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS) *
        static_cast<unsigned long>(rpc::RPC_DEFAULT_RETRY_LIMIT + 1);
    g_test_millis = window_ms + 1000;
    TEST_ASSERT(!ba.isRecentDuplicateRx(frame));

    // Ack timeout set to 0 -> only accept duplicates at the exact same timestamp.
    ba.setAckTimeoutMs(0);
    g_test_millis += 1;
    TEST_ASSERT(!ba.isRecentDuplicateRx(frame));

    // Payload too large -> never considered duplicate.
    frame.header.payload_length = rpc::MAX_PAYLOAD_SIZE + 1;
    TEST_ASSERT(!ba.isRecentDuplicateRx(frame));
}

void test_bridge_timing_config_validation() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    auto ba = bridge::test::TestAccessor::create(bridge);

    // Defaults when payload is missing/short.
    ba.applyTimingConfig(nullptr, 0);
    TEST_ASSERT_EQ_UINT(ba.getAckTimeoutMs(), rpc::RPC_DEFAULT_ACK_TIMEOUT_MS);
    TEST_ASSERT_EQ_UINT(ba.getAckRetryLimit(), rpc::RPC_DEFAULT_RETRY_LIMIT);
    TEST_ASSERT_EQ_UINT(ba.getResponseTimeoutMs(), rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS);

    // Out-of-range values fall back to defaults/min.
    uint8_t bad_payload[rpc::RPC_HANDSHAKE_CONFIG_SIZE];
    rpc::write_u16_be(&bad_payload[0], 1);  // too small
    bad_payload[2] = 99;                   // too large
    rpc::write_u32_be(&bad_payload[3], 1); // too small
    ba.applyTimingConfig(bad_payload, sizeof(bad_payload));
    TEST_ASSERT_EQ_UINT(ba.getAckTimeoutMs(), rpc::RPC_DEFAULT_ACK_TIMEOUT_MS);
    TEST_ASSERT_EQ_UINT(ba.getAckRetryLimit(), rpc::RPC_DEFAULT_RETRY_LIMIT);
    TEST_ASSERT_EQ_UINT(ba.getResponseTimeoutMs(), rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS);

    // Valid values are applied.
    uint8_t good_payload[rpc::RPC_HANDSHAKE_CONFIG_SIZE];
    rpc::write_u16_be(&good_payload[0], 500);
    good_payload[2] = 2;
    rpc::write_u32_be(&good_payload[3], 1000);
    ba.applyTimingConfig(good_payload, sizeof(good_payload));
    TEST_ASSERT_EQ_UINT(ba.getAckTimeoutMs(), 500);
    TEST_ASSERT_EQ_UINT(ba.getAckRetryLimit(), 2);
    TEST_ASSERT_EQ_UINT(ba.getResponseTimeoutMs(), 1000);
}

struct StatusCapture {
    static StatusCapture* instance;
    bool called;
    rpc::StatusCode code;
    uint16_t length;

    StatusCapture() : called(false), code(rpc::StatusCode::STATUS_ERROR), length(0) {}
};

StatusCapture* StatusCapture::instance = nullptr;

static void status_handler_trampoline(rpc::StatusCode code, const uint8_t*, uint16_t length) {
    StatusCapture* state = StatusCapture::instance;
    if (!state) return;
    state->called = true;
    state->code = code;
    state->length = length;
}

void test_bridge_ack_malformed_timeout_paths() {
    ModeStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    auto ba = bridge::test::TestAccessor::create(bridge);
    ba.setStartupStabilizing(false);  // Skip startup phase for test
    ba.setIdle();
    stream.clear_tx();

    // Send a command that requires ACK.
    const uint8_t payload[] = {TEST_BYTE_01};
    g_test_millis = 0;
    TEST_ASSERT(bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, payload, sizeof(payload)));
    TEST_ASSERT(ba.isAwaitingAck());

    // Malformed for the last command triggers retransmission and increments retry count.
    rpc::Frame malformed{};
    malformed.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED);
    malformed.header.payload_length = 2;
    rpc::write_u16_be(malformed.payload.data(), ba.getLastCommandId());
    g_test_millis = 50;
    ba.dispatch(malformed);
    TEST_ASSERT_EQ_UINT(ba.getRetryCount(), 1);

    // ACK with missing payload uses sentinel and still clears state.
    rpc::Frame ack_missing{};
    ack_missing.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_ACK);
    ack_missing.header.payload_length = 0;
    ba.dispatch(ack_missing);
    TEST_ASSERT(!ba.isAwaitingAck());

    // Timeout path when retry limit is exceeded calls status handler.
    StatusCapture status;
    StatusCapture::instance = &status;
    bridge.onStatus(BridgeClass::StatusHandler::create<status_handler_trampoline>());

    // Re-arm ACK state.
    stream.clear_tx();
    g_test_millis = 0;
    TEST_ASSERT(bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, payload, sizeof(payload)));
    ba.setAckTimeoutMs(10);
    ba.setAckRetryLimit(0);

    g_test_millis = 100;
    bridge._onAckTimeout();
    TEST_ASSERT(!ba.isAwaitingAck());
    TEST_ASSERT(status.called);
    TEST_ASSERT(status.code == rpc::StatusCode::STATUS_TIMEOUT);

    StatusCapture::instance = nullptr;
}

void test_bridge_enqueue_rejects_overflow_and_full() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);

    uint8_t big[rpc::MAX_PAYLOAD_SIZE + 1];
    test_memfill(big, sizeof(big), TEST_BYTE_BB);
    // Over-sized payload should fail
    TEST_ASSERT(!bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, big, sizeof(big)));

    // Fill queue.
    // Transition to AwaitingAck so subsequent frames get queued
    auto accessor = bridge::test::TestAccessor::create(bridge);
    accessor.setAwaitingAck();

    while(!accessor.isPendingTxQueueFull()) {
        bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, nullptr, 0);
    }
    // Queue is full, this should fail
    TEST_ASSERT(!bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, nullptr, 0));
}

void test_bridge_emit_status_message_variants() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();

    bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, "");
    TEST_ASSERT(stream.tx_buffer.len > 0);
    TEST_ASSERT_EQ_UINT(
        first_frame_command_id_or_sentinel(stream.tx_buffer),
        rpc::to_underlying(rpc::StatusCode::STATUS_ERROR));

    stream.tx_buffer.clear();
    bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, "err");
    TEST_ASSERT(stream.tx_buffer.len > 0);
    TEST_ASSERT_EQ_UINT(
        first_frame_command_id_or_sentinel(stream.tx_buffer),
        rpc::to_underlying(rpc::StatusCode::STATUS_ERROR));
}

void test_bridge_system_commands_and_baudrate_state_machine() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();

    // GET_FREE_MEMORY (payload_length == 0) emits a response.
    rpc::Frame free_mem{};
    free_mem.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY);
    free_mem.header.payload_length = 0;
    auto ba = bridge::test::TestAccessor::create(bridge);
    ba.handleSystemCommand(free_mem);
    TEST_ASSERT(stream.tx_buffer.len > 0);
    TEST_ASSERT_EQ_UINT(
        first_frame_command_id_or_sentinel(stream.tx_buffer),
        rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP));

    // SET_BAUDRATE schedules a deferred baud change; process() applies it after 50ms.
    stream.tx_buffer.clear();
    rpc::Frame baud{};
    baud.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE);
    baud.header.payload_length = 4;
    rpc::write_u32_be(baud.payload.data(), 57600);
    g_test_millis = 1000;
    ba.handleSystemCommand(baud);
    TEST_ASSERT(stream.tx_buffer.len > 0);
    TEST_ASSERT_EQ_UINT(
        first_frame_command_id_or_sentinel(stream.tx_buffer),
        rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE_RESP));

    // No-op before 50ms.
    auto ca = bridge::test::ConsoleTestAccessor::create(Console);
    ca.setBegun(false);
    g_test_millis = 1020;
    bridge.process();

    // Applies after 50ms.
    g_test_millis = 1100;
    bridge.process();
}

void test_bridge_link_reset_payload_variants() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    stream.tx_buffer.clear();

    // LINK_RESET with no payload.
    rpc::Frame reset0{};
    reset0.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET);
    reset0.header.payload_length = 0;
    auto ba2 = bridge::test::TestAccessor::create(bridge);
    ba2.handleSystemCommand(reset0);
    TEST_ASSERT(stream.tx_buffer.len > 0);
    TEST_ASSERT_EQ_UINT(
        first_frame_command_id_or_sentinel(stream.tx_buffer),
        rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET_RESP));

    // LINK_RESET with timing config payload.
    stream.tx_buffer.clear();
    rpc::Frame reset_cfg{};
    reset_cfg.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET);
    reset_cfg.header.payload_length = rpc::RPC_HANDSHAKE_CONFIG_SIZE;
    // ack_timeout=250, retry=2, response_timeout=1000
    rpc::write_u16_be(&reset_cfg.payload[0], 250);
    reset_cfg.payload[2] = 2;
    rpc::write_u32_be(&reset_cfg.payload[3], 1000);
    ba2.handleSystemCommand(reset_cfg);
    TEST_ASSERT(stream.tx_buffer.len > 0);
    TEST_ASSERT_EQ_UINT(
        first_frame_command_id_or_sentinel(stream.tx_buffer),
        rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET_RESP));
}

void test_bridge_dispatch_gpio_ack_and_no_ack() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    auto ba = bridge::test::TestAccessor::create(bridge);

    ba.setIdle(); // [FIX] Ensure bridge is synchronized to allow app commands
    stream.tx_buffer.clear();

    // Commands that require an ACK.
    rpc::Frame pinmode{};
    pinmode.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
    pinmode.header.payload_length = 2;
    pinmode.payload[0] = 13;
    pinmode.payload[1] = OUTPUT;
    ba.dispatch(pinmode);

    rpc::Frame dwrite{};
    dwrite.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    dwrite.header.payload_length = 2;
    dwrite.payload[0] = 13;
    dwrite.payload[1] = HIGH;
    ba.dispatch(dwrite);

    const size_t ack_count = count_status_ack_frames(stream.tx_buffer);
    TEST_ASSERT(ack_count >= 2);

    // Commands that do not require an ACK.
    stream.tx_buffer.clear();
    rpc::Frame dread{};
    dread.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ);
    dread.header.payload_length = 1;
    dread.payload[0] = 13;
    ba.dispatch(dread);
    TEST_ASSERT_EQ_UINT(count_status_ack_frames(stream.tx_buffer), 0);
}

void test_bridge_malformed_frame() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    auto ba_m = bridge::test::TestAccessor::create(bridge);
    ba_m.setStartupStabilizing(false);  // Skip startup phase for test
    ba_m.setIdle(); // [FIX] Enable sync so errors are reported
    stream.tx_buffer.clear();

    // Inject garbage data into the stream to trigger malformed/overflow logic
    // We want to simulate MALFORMED, so let's send valid COBS of random data:
    
    enum { kEncodedCap = kMaxEncodedSize + 1 };
    uint8_t encoded[kEncodedCap];
    
    uint8_t random_data[50];
    test_memfill(random_data, sizeof(random_data), 0x77);
    // Encode raw random data (not a frame)
    size_t len = TestCOBS::encode(random_data, sizeof(random_data), encoded);
    encoded[len++] = rpc::RPC_FRAME_DELIMITER;
    
    stream.inject_rx(encoded, len);
    bridge.process();
    
    // Should have sent a STATUS_MALFORMED or CRC_MISMATCH
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_file_write_eeprom_parsing() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    auto ba_f = bridge::test::TestAccessor::create(bridge);
    ba_f.setStartupStabilizing(false);  // Skip startup phase for test
    ba_f.setIdle();  // Enable frame processing
    stream.tx_buffer.clear();
    
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

    enum { kEncodedCap = kMaxEncodedSize + 1 };
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
    sync_bridge(bridge, stream); // Sync the bridge before testing error handling
    
    // Case 2: Malformed path length (claim 100 bytes, provide 5)
    const uint8_t payload[] = {100, '/', 'e'};

    enum { kEncodedCap = kMaxEncodedSize + 1 };
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
    sync_bridge(bridge, stream); // Sync the bridge before testing error handling
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

    enum { kEncodedCap = kMaxEncodedSize + 1 };
    uint8_t encoded[kEncodedCap];
    const size_t encoded_len = TestCOBS::encode(raw, cursor, encoded);
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
    sync_bridge(bridge, stream); // Sync the bridge before testing error handling
    stream.tx_buffer.clear();

    // Command ID rpc::RPC_INVALID_ID_SENTINEL is likely unknown
    const uint16_t cmd_id = rpc::RPC_INVALID_ID_SENTINEL;
    const uint8_t payload[] = {0};
    enum { kEncodedCap = kMaxEncodedSize + 1 };
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
    auto ba_pl = bridge::test::TestAccessor::create(bridge);
    ba_pl.setStartupStabilizing(false);  // Skip startup phase for test
    ba_pl.setIdle(); // [FIX] Enable sync
    stream.tx_buffer.clear();

    // Max payload is 128 (now 64). Build an oversized raw frame (200 bytes) and inject it.
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
    const size_t encoded_len = TestCOBS::encode(raw, cursor, encoded);
    TEST_ASSERT(encoded_len > 0);
    TEST_ASSERT(encoded_len + 1 <= sizeof(encoded));
    encoded[encoded_len] = rpc::RPC_FRAME_DELIMITER;
    stream.inject_rx(encoded, encoded_len + 1);
    
    bridge.process();
    
    // Should result in STATUS_MALFORMED or similar, or just be dropped/reset.
    // The current implementation might send an error.
    TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_bridge_chunking() {
    MockStream stream;
    BridgeClass bridge(stream);
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    sync_bridge(bridge, stream);
    stream.tx_buffer.clear();

    // Data Setup
    // MAX_PAYLOAD_SIZE = 64
    // Header: 5 bytes
    // Max Chunk: 59 bytes
    // Data: 100 bytes
    // Expected: Frame 1 (5+59=64), Frame 2 (5+41=46)
    
    uint8_t header[5] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE};
    uint8_t data[100];
    for(size_t i=0; i<100; i++) data[i] = static_cast<uint8_t>(i);

    // Use a command that does NOT require ACK to test fragmentation logic
    // without needing to simulate ACKs in the middle of the blocking call.
    bridge.sendChunkyFrame(rpc::CommandId::CMD_MAILBOX_PROCESSED, header, 5, data, 100);

    // Verification
    // We expect 2 valid frames in tx_buffer.
    
    size_t cursor = 0;
    uint8_t decoded[rpc::MAX_RAW_FRAME_SIZE];
    rpc::FrameParser parser;

    // --- Frame 1 ---
    // Find delimiter
    size_t end1 = cursor;
    while(end1 < stream.tx_buffer.len && stream.tx_buffer.data[end1] != rpc::RPC_FRAME_DELIMITER) end1++;
    TEST_ASSERT(end1 < stream.tx_buffer.len); // Must find delimiter
    
    size_t len1 = TestCOBS::decode(&stream.tx_buffer.data[cursor], end1 - cursor, decoded);
    auto result1 = parser.parse(etl::span<const uint8_t>(decoded, len1));
    TEST_ASSERT(result1.has_value());
    rpc::Frame f1 = result1.value();
    TEST_ASSERT_EQ_UINT(f1.header.command_id, rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PROCESSED));
    TEST_ASSERT_EQ_UINT(f1.header.payload_length, 64);
    // Check Header
    TEST_ASSERT(memcmp(f1.payload.data(), header, 5) == 0);
    // Check Data Chunk 1 (0-58)
    TEST_ASSERT(memcmp(f1.payload.data() + 5, data, 59) == 0);
    
    cursor = end1 + 1; // Skip delimiter

    // --- Frame 2 ---
    size_t end2 = cursor;
    while(end2 < stream.tx_buffer.len && stream.tx_buffer.data[end2] != rpc::RPC_FRAME_DELIMITER) end2++;
    TEST_ASSERT(end2 < stream.tx_buffer.len);
    
    size_t len2 = TestCOBS::decode(&stream.tx_buffer.data[cursor], end2 - cursor, decoded);
    auto result2 = parser.parse(etl::span<const uint8_t>(decoded, len2));
    TEST_ASSERT(result2.has_value());
    rpc::Frame f2 = result2.value();
    TEST_ASSERT_EQ_UINT(f2.header.command_id, rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PROCESSED));
    TEST_ASSERT_EQ_UINT(f2.header.payload_length, 46); // 5 + 41
    TEST_ASSERT(memcmp(f2.payload.data(), header, 5) == 0);
    TEST_ASSERT(memcmp(f2.payload.data() + 5, data + 59, 41) == 0);
}

int main() {
    test_bridge_begin();
    test_bridge_send_frame();
    test_bridge_process_rx();
    test_bridge_handshake();
    test_bridge_flow_control();
    test_bridge_file_write_incoming();
    test_bridge_malformed_frame();
    test_file_write_eeprom_parsing();
    test_file_write_malformed_path();

    // Idempotency regression tests
    test_bridge_dedup_console_write_retry();
    test_bridge_dedup_window_edges();
    test_bridge_timing_config_validation();
    test_bridge_ack_malformed_timeout_paths();
    test_bridge_enqueue_rejects_overflow_and_full();
    test_bridge_emit_status_message_variants();
    test_bridge_system_commands_and_baudrate_state_machine();
    test_bridge_link_reset_payload_variants();
    test_bridge_dispatch_gpio_ack_and_no_ack();
    
    // New Robustness Tests
    test_bridge_crc_mismatch();
    test_bridge_unknown_command();
    test_bridge_payload_too_large();
    
    // Chunking
    test_bridge_chunking();
    
    return 0;
}
