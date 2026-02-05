#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rle.h"
#include "protocol/security.h"
#include "arduino/StringUtils.h"
#include "etl/error_handler.h"
#include "test_support.h"

// Mocks y Stubs Globales
HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
ConsoleClass Console;
#if BRIDGE_ENABLE_DATASTORE
DataStoreClass DataStore;
#endif
#if BRIDGE_ENABLE_MAILBOX
MailboxClass Mailbox;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
FileSystemClass FileSystem;
#endif
#if BRIDGE_ENABLE_PROCESS
ProcessClass Process;
#endif

namespace {

class CaptureStream : public Stream {
public:
    ByteBuffer<4096> tx;
    size_t write(uint8_t c) override { tx.push(c); return 1; }
    size_t write(const uint8_t* b, size_t s) override { tx.append(b, s); return s; }
    int available() override { return 0; }
    int read() override { return -1; }
    int peek() override { return -1; }
    void flush() override {}
};

void setup_env(CaptureStream& stream) {
    Bridge.~BridgeClass();
    new (&Bridge) BridgeClass(stream);
    Bridge.begin();
    Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete();
}

// --- TARGET: rpc_frame.cpp Gaps ---
void test_rpc_frame_gaps() {
    printf("  -> Testing rpc_frame_gaps\n");
    rpc::FrameParser parser;
    
    // [SIL-2] etl::expected API - Gap: parse with size too small for CRC
    uint8_t short_data[] = {0x01, 0x02};
    auto result1 = parser.parse(short_data, sizeof(short_data));
    assert(!result1.has_value());
    assert(result1.error() == rpc::FrameError::MALFORMED);

    // Gap: parse with crc_start < sizeof(FrameHeader)
    uint8_t header_short[] = {0x02, 0x00, 0x00, 0x40, 0x00, 0x00, 0x00, 0x00}; // 8 bytes, header is 5, crc 4. 8-4 = 4 < 5.
    auto result2 = parser.parse(header_short, sizeof(header_short));
    assert(!result2.has_value());

    // Gap: parse with invalid version
    uint8_t bad_version[] = {0xFF, 0x00, 0x00, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}; 
    uint32_t c = crc32_ieee(bad_version, 6);
    rpc::write_u32_be(&bad_version[6], c);
    auto result3 = parser.parse(bad_version, 10);
    assert(!result3.has_value());

    // Gap: parse with payload_length > max_size
    uint8_t bad_len[] = {0x02, 0xFF, 0xFF, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00};
    c = crc32_ieee(bad_len, 5);
    rpc::write_u32_be(&bad_len[5], c);
    auto result4 = parser.parse(bad_len, 9);
    assert(!result4.has_value());

    // Gap: parse with (sizeof(FrameHeader) + payload_length) != crc_start
    uint8_t len_mismatch[] = {0x02, 0x00, 0x05, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}; 
    c = crc32_ieee(len_mismatch, 6);
    rpc::write_u32_be(&len_mismatch[6], c);
    auto result5 = parser.parse(len_mismatch, 10);
    assert(!result5.has_value());

    // Gap: build with payload_len > MAX_PAYLOAD_SIZE
    rpc::FrameBuilder builder;
    uint8_t out[rpc::MAX_RAW_FRAME_SIZE];
    assert(builder.build(out, sizeof(out), 0x40, nullptr, rpc::MAX_PAYLOAD_SIZE + 1) == 0);

    // Gap: build with buffer too small
    assert(builder.build(out, 5, 0x40, nullptr, 0) == 0);
}

// --- TARGET: Bridge.cpp Gaps ---
void test_bridge_extra_gaps() {
    printf("  -> Testing bridge_extra_gaps\n");
    CaptureStream stream;
    setup_env(stream);

    // Gap: _emitStatus with null message
    Bridge._emitStatus(rpc::StatusCode::STATUS_OK, (const char*)nullptr);
    
    // Gap: _emitStatus with Flash string
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("FlashError"));

    // Gap: handleResponse STATUS_MALFORMED
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED);
    f.header.payload_length = 2;
    rpc::write_u16_be(f.payload.data(), 0x40);
    Bridge.dispatch(f);

    // Gap: dispatch CMD_UNKNOWN
    f.header.command_id = 0x9999;
    f.header.payload_length = 0;
    Bridge.dispatch(f);

    // [SIL-2] etl::expected API - Gap: process() with parse errors via _last_parse_error
    Bridge._last_parse_error = rpc::FrameError::OVERFLOW;
    Bridge.process();

    // Gap: process() with CRC_MISMATCH error
    Bridge._last_parse_error = rpc::FrameError::CRC_MISMATCH;
    Bridge.process();

    // Gap: process() with MALFORMED error
    Bridge._last_parse_error = rpc::FrameError::MALFORMED;
    Bridge.process();

    // Gap: _applyTimingConfig with null payload or short length
    Bridge._applyTimingConfig(nullptr, 0);
    uint8_t short_config[] = {0x01};
    Bridge._applyTimingConfig(short_config, 1);

    // Gap: _requiresAck default case
    assert(!Bridge._requiresAck(rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION)));

    // Gap: sendFrame Status code
    Bridge.sendFrame(rpc::StatusCode::STATUS_ACK);

    // Gap: sendFrame while not synchronized (allowed commands)
    Bridge._fsm.resetFsm();
    assert(Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION_RESP));
    assert(Bridge.sendFrame(rpc::CommandId::CMD_LINK_SYNC_RESP));
    assert(Bridge.sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP));
    assert(!Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE));
    Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete();

    // Gap: _handleAck with invalid ID
    Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete(); Bridge._fsm.sendCritical();
    Bridge._last_command_id = 0x60;
    Bridge._handleAck(rpc::RPC_INVALID_ID_SENTINEL);
    assert(!Bridge._fsm.isAwaitingAck());

    // Gap: _handleMalformed with invalid ID
    Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete(); Bridge._fsm.sendCritical();
    Bridge._last_command_id = 0x60;
    Bridge._handleMalformed(rpc::RPC_INVALID_ID_SENTINEL);

    // Gap: _computeHandshakeTag with null nonce
    uint8_t tag[32];
    Bridge._computeHandshakeTag(nullptr, 0, tag);

    // Gap: begin with secret
    Bridge.begin(115200, "mysecret");
    assert(Bridge._shared_secret.size() == 8);

    // Gap: begin with secret > capacity
    char long_secret[64];
    etl::fill_n(long_secret, 63, 'S');
    long_secret[63] = '\0';
    Bridge.begin(115200, long_secret);
    assert(Bridge._shared_secret.size() == 32);

    // Gap: flushStream with hardware serial
    Bridge.flushStream(); // Bridge was initialized with Serial1

    // Gap: CMD_LINK_SYNC with response_length > MAX_PAYLOAD_SIZE
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    f.header.payload_length = 120; // 120 + 32 > 128
    Bridge._handleSystemCommand(f);

    // Gap: _isRecentDuplicateRx branches
    Bridge._last_rx_crc = 0x1234;
    Bridge._last_rx_crc_millis = millis();
    f.crc = 0x1234;
    assert(Bridge._isRecentDuplicateRx(f) == false); // elapsed < ack_timeout

    // Gap: _sendFrame with Fault state
    Bridge._fsm.cryptoFault();
    assert(!Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION));
    Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete();

    // Gap: _flushPendingTxQueue raw_len == 0 (force error)
    Bridge._pending_tx_queue.clear();
    BridgeClass::PendingTxFrame pf;
    pf.command_id = 0x40;
    pf.payload_length = rpc::MAX_PAYLOAD_SIZE + 1; // Invalid
    Bridge._pending_tx_queue.push(pf);
    Bridge._flushPendingTxQueue();
}

// --- TARGET: Console.cpp Gaps ---
void test_console_extra_gaps() {
    printf("  -> Testing console_extra_gaps\n");
    CaptureStream stream;
    setup_env(stream);
    Console.begin();

    // Gap: write(c)
    Console.write('X');

    // Gap: write(c) when full
    while(!Console._tx_buffer.full()) Console._tx_buffer.push_back('A');
    Console.write('B');

    // Gap: write(buf, size) when not empty
    Console._tx_buffer.clear();
    Console.write('A');
    Console.write((const uint8_t*)"hello", 5);

    // Gap: peek()
    Console._rx_buffer.push('P');
    assert(Console.peek() == 'P');

    // Gap: available()
    assert(Console.available() == 1);

    // Gap: read() empty
    Console._rx_buffer.clear();
    assert(Console.read() == -1);

    // Gap: flush() not begun
    Console._begun = false;
    Console.flush();
    Console._begun = true;

    // Gap: _push empty or 0 capacity
    Console._push(nullptr, 0);
    
    // Gap: _push when full
    while(!Console._rx_buffer.full()) Console._rx_buffer.push('Z');
    uint8_t data = 'X';
    Console._push(&data, 1);
}

// --- TARGET: DataStore.cpp Gaps ---
void test_datastore_extra_gaps() {
    printf("  -> Testing test_datastore_extra_gaps\n");
    CaptureStream stream;
    setup_env(stream);

    // Gap: put with null key/value
    DataStore.put(nullptr, "val");
    DataStore.put("key", nullptr);

    // Gap: put with too long key
    char long_key[rpc::RPC_MAX_DATASTORE_KEY_LENGTH + 10];
    etl::fill_n(long_key, sizeof(long_key), 'k');
    long_key[sizeof(long_key)-1] = '\0';
    DataStore.put(long_key, "val");

    // Gap: requestGet with null key
    DataStore.requestGet(nullptr);

    // Gap: requestGet with too long key
    DataStore.requestGet(long_key);

    // Gap: handleResponse with other command
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    DataStore.handleResponse(f);

    // Gap: handleResponse CMD_DATASTORE_GET_RESP without handler
    DataStore.onDataStoreGetResponse(nullptr);
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
    f.header.payload_length = 5;
    f.payload[0] = 2; // key len
    f.payload[1] = 'k'; f.payload[2] = '1';
    f.payload[3] = 2; // val len
    f.payload[4] = 'v'; f.payload[5] = '1';
    DataStore.handleResponse(f);

    // Gap: _popPendingDatastoreKey empty
    DataStore._pending_datastore_keys.clear();
    const char* key = DataStore._popPendingDatastoreKey();
    assert(key != nullptr);
    assert(strlen(key) == 0);

    // Gap: _trackPendingDatastoreKey empty or too long
    assert(!DataStore._trackPendingDatastoreKey(""));
    assert(!DataStore._trackPendingDatastoreKey(long_key));
}

// --- TARGET: Mailbox.cpp Gaps ---
void test_mailbox_extra_gaps() {
    printf("  -> Testing mailbox_extra_gaps\n");
    CaptureStream stream;
    setup_env(stream);

    // Gap: send(char*) with null or empty
    Mailbox.send((const char*)nullptr);
    Mailbox.send("");

    // Gap: send with large message
    char large_msg[rpc::MAX_PAYLOAD_SIZE + 10];
    etl::fill_n(large_msg, sizeof(large_msg), 'M');
    large_msg[sizeof(large_msg)-1] = '\0';
    Mailbox.send(large_msg);

    // Gap: handleResponse CMD_MAILBOX_READ_RESP without handler
    Mailbox.onMailboxMessage(nullptr);
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
    Mailbox.handleResponse(f);

    // Gap: handleResponse CMD_MAILBOX_AVAILABLE_RESP without handler
    Mailbox.onMailboxAvailableResponse(nullptr);
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
    Mailbox.handleResponse(f);
}

// --- TARGET: Process.cpp Gaps ---
void test_process_extra_gaps() {
    printf("  -> Testing process_extra_gaps\n");
    CaptureStream stream;
    setup_env(stream);

    // Gap: runAsync with null/empty
    Process.runAsync(nullptr);
    Process.runAsync("");

    // Gap: runAsync too large
    char long_cmd[rpc::MAX_PAYLOAD_SIZE + 5];
    etl::fill_n(long_cmd, sizeof(long_cmd), 'c');
    long_cmd[sizeof(long_cmd)-1] = '\0';
    Process.runAsync(long_cmd);

    // Gap: poll with invalid PID
    Process.poll(-1);

    // Gap: poll with full pending queue
    for(int i=0; i<BRIDGE_MAX_PENDING_PROCESS_POLLS; ++i) {
        Process._pushPendingProcessPid(i+1);
    }
    Process.poll(99);

    // Gap: handleResponse CMD_PROCESS_RUN_RESP without handler
    Process.onProcessRunResponse(nullptr);
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_RESP);
    f.header.payload_length = 5;
    f.payload[0] = 0x30;
    rpc::write_u16_be(&f.payload[1], 0);
    rpc::write_u16_be(&f.payload[3], 0);
    Process.handleResponse(f);

    // Gap: handleResponse CMD_PROCESS_RUN_ASYNC_RESP with handler
    Process.onProcessRunAsyncResponse([](int16_t pid){ (void)pid; });
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
    f.header.payload_length = 2;
    rpc::write_u16_be(f.payload.data(), 123);
    Process.handleResponse(f);

    // Gap: handleResponse CMD_PROCESS_POLL_RESP without handler
    Process.onProcessPollResponse(nullptr);
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
    f.header.payload_length = 6;
    f.payload[0] = 0x30;
    f.payload[1] = 0;
    rpc::write_u16_be(&f.payload[2], 0);
    rpc::write_u16_be(&f.payload[4], 0);
    Process.handleResponse(f);

    // Gap: handleResponse with other command
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    Process.handleResponse(f);

    // Gap: _popPendingProcessPid empty
    Process._pending_process_pids.clear();
    assert(Process._popPendingProcessPid() == rpc::RPC_INVALID_ID_SENTINEL);
}

// --- TARGET: rle.h Gaps ---
void test_rle_gaps() {
    printf("  -> Testing rle_gaps\n");
    uint8_t src[16];
    etl::fill_n(src, 16, uint8_t{0x41}); // Run of 'A'
    uint8_t dst[64];
    
    // Gap: encode/decode null or 0
    assert(rle::encode(nullptr, 1, dst, 10) == 0);
    assert(rle::decode(nullptr, 1, dst, 10) == 0);

    // Gap: encode ESCAPE_BYTE cases
    uint8_t src_esc[3] = {rle::ESCAPE_BYTE, rle::ESCAPE_BYTE, rle::ESCAPE_BYTE};
    assert(rle::encode(src_esc, 1, dst, 10) > 0); // Single escape
    assert(rle::encode(src_esc, 2, dst, 10) > 0); // Double escape
    assert(rle::encode(src_esc, 3, dst, 10) > 0); // Triple escape

    // Gap: encode/decode overflow
    assert(rle::encode(src, 16, dst, 1) == 0);
    uint8_t enc[10] = {rle::ESCAPE_BYTE, 0, 0x01};
    assert(rle::decode(enc, 3, dst, 1) == 0);

    // Gap: should_compress with beneficial run
    assert(rle::should_compress(src, 16));
}

// --- TARGET: security.h Gaps ---
void test_security_gaps() {
    printf("  -> Testing security_gaps\n");
    uint8_t ikm[32] = {0};
    uint8_t okm[32];
    // Test HKDF with null salt (uses library's ::hkdf<SHA256>)
    rpc::security::hkdf_sha256(ikm, 32, nullptr, 0, nullptr, 0, okm, 32);
}

// --- TARGET: StringUtils.h Gaps ---
void test_string_utils_gaps() {
    printf("  -> Testing string_utils_gaps\n");
    // Gap: measure_bounded_cstring with null or 0 max_len
    auto info = measure_bounded_cstring(nullptr, 10);
    assert(info.length == 0 && info.overflowed);
    info = measure_bounded_cstring("test", 0);
    assert(info.length == 0 && info.overflowed);
}

} // namespace

int main() {
    printf("ARDUINO 100%% COVERAGE TEST START\n");
    test_rpc_frame_gaps();
    test_bridge_extra_gaps();
    test_console_extra_gaps();
    test_datastore_extra_gaps();
    test_mailbox_extra_gaps();
    test_process_extra_gaps();
    test_rle_gaps();
    test_security_gaps();
    test_string_utils_gaps();
    printf("ARDUINO 100%% COVERAGE TEST END\n");
    return 0;
}