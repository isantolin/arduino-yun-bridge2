#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "protocol/rle.h"
#include "fsm/bridge_fsm.h"
#include "protocol/rpc_structs.h"
#include "router/command_router.h"
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

BridgeClass Bridge(Serial1);

using namespace bridge::fsm;
using namespace bridge::test;
using namespace bridge::router;

void test_fsm_only() {
    StateUnsynchronized s_un;
    TEST_ASSERT(s_un.on_enter_state() == STATE_UNSYNCHRONIZED);
    
    // Line 88-89: Handshake failed in Unsynchronized
    TEST_ASSERT(s_un.on_event(EvHandshakeFailed()) == STATE_FAULT);
    // Line 96-97: Crypto fault in Unsynchronized
    TEST_ASSERT(s_un.on_event(EvCryptoFault()) == STATE_FAULT);
}

void test_rle_gaps() {
    // Line 97: Encode overflow
    uint8_t src[] = {0xFF, 0xFF};
    uint8_t dst[2];
    TEST_ASSERT(rle::encode(etl::span<const uint8_t>(src, 2), etl::span<uint8_t>(dst, 2)) == 0);

    // Line 99-105: runs of 0xFF < MIN_RUN_LENGTH
    uint8_t src2[] = {0xFF, 0xFF, 0xFF};
    uint8_t dst2[10];
    size_t len = rle::encode(etl::span<const uint8_t>(src2, 3), etl::span<uint8_t>(dst2, 10));
    TEST_ASSERT(len > 0);
    
    // Line 147: Decode special 255
    uint8_t decoded[10];
    size_t dlen = rle::decode(etl::span<const uint8_t>(dst2, len), etl::span<uint8_t>(decoded, 10));
    TEST_ASSERT(dlen == 3);
    TEST_ASSERT(decoded[0] == 0xFF);

    // Line 100: Single 0xFF escape
    uint8_t src4[] = {0xFF};
    uint8_t dst4[10];
    size_t len4 = rle::encode(etl::span<const uint8_t>(src4, 1), etl::span<uint8_t>(dst4, 10));
    TEST_ASSERT(len4 == 3);
    TEST_ASSERT(dst4[1] == 255);
    
    // Line 147: Decode single 0xFF
    uint8_t decoded4[10];
    size_t dlen4 = rle::decode(etl::span<const uint8_t>(dst4, len4), etl::span<uint8_t>(decoded4, 10));
    TEST_ASSERT(dlen4 == 1);
    TEST_ASSERT(decoded4[0] == 0xFF);

    // Line 193-195: should_compress escape path
    uint8_t src3[] = {0xFF, 1, 2, 3, 4, 5, 6, 7, 8};
    rle::should_compress(etl::span<const uint8_t>(src3, sizeof(src3)));
}

void test_rpc_structs_gaps() {
    // VersionResponse encode
    rpc::payload::VersionResponse vr{1, 2};
    uint8_t buf[2];
    vr.encode(buf);
    TEST_ASSERT(buf[0] == 1);
    
    // VersionResponse parse
    uint8_t vbuf[] = {3, 4};
    rpc::payload::VersionResponse vr2 = rpc::payload::VersionResponse::parse(vbuf);
    TEST_ASSERT(vr2.major == 3);

    // AnalogReadResponse encode
    rpc::payload::AnalogReadResponse ar{1023};
    uint8_t buf2[2];
    ar.encode(buf2);
    TEST_ASSERT(rpc::read_u16_be(buf2) == 1023);

    // AnalogWrite parse
    uint8_t awbuf[] = {5, 200};
    rpc::payload::AnalogWrite aw = rpc::payload::AnalogWrite::parse(awbuf);
    TEST_ASSERT(aw.pin == 5);

    // MailboxPush parse
    uint8_t mpbuf[] = {0, 3, 'A', 'B', 'C'};
    rpc::payload::MailboxPush mp = rpc::payload::MailboxPush::parse(mpbuf);
    TEST_ASSERT(mp.length == 3);

    // AckPacket parse
    uint8_t apbuf[] = {0, 0x42};
    rpc::payload::AckPacket ap = rpc::payload::AckPacket::parse(apbuf);
    TEST_ASSERT(ap.command_id == 0x42);

    // Payload::parse template paths
    rpc::Frame f;
    f.header.payload_length = 1;
    // VersionResponse::SIZE is 2
    auto res = rpc::Payload::parse<rpc::payload::VersionResponse>(f);
    TEST_ASSERT(res.has_value() == false);
    
    // Line 261: Success return
    f.header.payload_length = 2;
    auto res2 = rpc::Payload::parse<rpc::payload::VersionResponse>(f);
    TEST_ASSERT(res2.has_value() == true);

    // Line 289-290: MailboxPush error paths
    f.header.payload_length = 1;
    auto res3 = rpc::Payload::parse<rpc::payload::MailboxPush>(f);
    TEST_ASSERT(res3.has_value() == false);
    
    uint8_t mbuf[2] = {0, 10}; // length 10
    etl::copy_n(mbuf, 2, f.payload.data());
    f.header.payload_length = 5; // < 10+2
    auto res4 = rpc::Payload::parse<rpc::payload::MailboxPush>(f);
    TEST_ASSERT(res4.has_value() == false);
}

void test_bridge_writer_gaps() {
    BridgeWriter writer;
    uint8_t header[rpc::MAX_PAYLOAD_SIZE + 1] = {0};
    
    // Line 117: Header too large
    TEST_ASSERT(writer.send(rpc::CommandId::CMD_CONSOLE_WRITE, header, sizeof(header), nullptr, 0) == false);

    // Line 123-130: Data length zero
    auto ba = TestAccessor::create(Bridge);
    ba.setIdle();
    uint8_t small_header[2] = {1, 2};
    TEST_ASSERT(writer.send(rpc::CommandId::CMD_CONSOLE_WRITE, small_header, 2, nullptr, 0) == true);

    // Line 135-150: Data length > 0 (Chunking)
    uint8_t large_data[100];
    memset(large_data, 'D', 100);
    TEST_ASSERT(writer.send(rpc::CommandId::CMD_CONSOLE_WRITE, small_header, 2, large_data, 100) == true);

    // Line 145: send failure + lost sync
    ba.setIdle();
    ba.setAwaitingAck();
    ba.pushPendingTxFrame(0, 0); // Fill queue
    ba.pushPendingTxFrame(0, 0); 
    ba.pushPendingTxFrame(0, 0); 
    ba.pushPendingTxFrame(0, 0); 
    // Now sendFrame will fail because queue is full.
    // We must manually trigger lost sync inside the loop?
    // Hard to do without a custom sendFrame mock.
}

void test_bridge_core_gaps() {
    auto ba = TestAccessor::create(Bridge);
    
    // Line 270: BridgeFsm::handshakeFailed()
    ba.fsmResetFsm();
    ba.fsmHandshakeFailed();
    TEST_ASSERT(ba.isFault());

    // Line 187-189: Secret too long
    uint8_t long_secret[100];
    memset(long_secret, 'A', sizeof(long_secret));
    // Re-initialize to ensure begin() path is hit
    Bridge.begin(115200, etl::string_view((const char*)long_secret, 100), 100);
    TEST_ASSERT(ba.sharedSecretSize() == 32); 

    // Line 302, 308, 311: Parse error coverage
    ba.setIdle();
    ba.setLastParseError(rpc::FrameError::CRC_MISMATCH);
    Bridge.process(); 
    
    ba.setLastParseError(rpc::FrameError::MALFORMED);
    Bridge.process();
    
    ba.setLastParseError(rpc::FrameError::OVERFLOW);
    Bridge.process();

    // Line 870-871: String command edge cases
    TEST_ASSERT(Bridge.sendStringCommand(rpc::CommandId::CMD_DATASTORE_GET, "", 10) == false);
    TEST_ASSERT(Bridge.sendStringCommand(rpc::CommandId::CMD_DATASTORE_GET, "too_long", 5) == false);

    // Line 893: KeyVal command edge cases
    TEST_ASSERT(Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_PUT, "", 10, "val", 10) == false);

    // Line 914: Chunky frame header too large
    uint8_t header[rpc::MAX_PAYLOAD_SIZE + 1];
    TEST_ASSERT(Bridge.sendChunkyFrame(rpc::CommandId::CMD_CONSOLE_WRITE, header, sizeof(header), nullptr, 0) == false);

    // Line 1029: Malformed retransmission
    ba.setAwaitingAck();
    ba.pushPendingTxFrame(rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION), 0);
    ba.handleMalformed(rpc::RPC_INVALID_ID_SENTINEL);
    TEST_ASSERT(ba.getRetryCount() == 1);

    // Line 440: CMD_LINK_SYNC bad length
    rpc::Frame f_sync;
    f_sync.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    f_sync.header.payload_length = 1; // Wrong
    ba.handleSystemCommand(f_sync);
}

static bool g_timeout_called = false;
static void timeout_trampoline(rpc::StatusCode status, const uint8_t*, uint16_t) {
    if (status == rpc::StatusCode::STATUS_TIMEOUT) g_timeout_called = true;
}

void test_bridge_core_timeout_status() {
    auto ba = TestAccessor::create(Bridge);
    ba.setIdle();
    g_timeout_called = false;
    Bridge.onStatus(BridgeClass::StatusHandler::create<timeout_trampoline>());
    ba.setAckRetryLimit(0);
    ba.setAwaitingAck(); // Force state
    ba.onAckTimeout();
    TEST_ASSERT(g_timeout_called == true);
    
    // Line 1168-1169: Deduplication window edge
    rpc::Frame f;
    f.crc = 0x12345678;
    ba.setLastRxCrc(f.crc);
    ba.setLastRxCrcMillis(1000);
    ba.setAckTimeoutMs(500);
    g_test_millis = 1200; // elapsed = 200 < 500
    TEST_ASSERT(ba.isRecentDuplicateRx(f) == false);
}

void test_fsm_gaps_more() {
    // FSM Unknown events coverage
    StateUnsynchronized s_un;
    TEST_ASSERT(s_un.on_event_unknown(EvReset()) == etl::ifsm_state::No_State_Change);
    
    StateSynchronized s_sync;
    TEST_ASSERT(s_sync.on_enter_state() == STATE_SYNCHRONIZED);
    TEST_ASSERT(s_sync.on_event(EvReset()) == STATE_UNSYNCHRONIZED);
    TEST_ASSERT(s_sync.on_event(EvCryptoFault()) == STATE_FAULT);
    TEST_ASSERT(s_sync.on_event(EvHandshakeComplete()) == etl::ifsm_state::No_State_Change);
    TEST_ASSERT(s_sync.on_event_unknown(EvReset()) == etl::ifsm_state::No_State_Change);

    StateAwaitingAck s_ack;
    TEST_ASSERT(s_ack.on_event_unknown(EvReset()) == etl::ifsm_state::No_State_Change);
    
    StateFault s_fault;
    TEST_ASSERT(s_fault.on_event_unknown(EvReset()) == etl::ifsm_state::No_State_Change);
    
    auto ba = TestAccessor::create(Bridge);
    ba.fsmResetFsm();
    ba.fsmHandshakeComplete(); // Transition to Idle
    ba.fsmSendCritical();      // Transition to AwaitingAck
    TEST_ASSERT(ba.isAwaitingAck());
}

void test_router_gaps() {
    // Line 145: virtual ~ICommandHandler
    class DummyHandler : public ICommandHandler {
    public:
        void onStatusCommand(const CommandContext&) override {}
        void onSystemCommand(const CommandContext&) override {}
        void onGpioCommand(const CommandContext&) override {}
        void onConsoleCommand(const CommandContext&) override {}
        void onDataStoreCommand(const CommandContext&) override {}
        void onMailboxCommand(const CommandContext&) override {}
        void onFileSystemCommand(const CommandContext&) override {}
        void onProcessCommand(const CommandContext&) override {}
        void onUnknownCommand(const CommandContext&) override {}
    };
    ICommandHandler* h = new DummyHandler();
    delete h;
    
    // Line 215, 217: on_receive_unknown
    CommandRouter router;
    router.on_receive_unknown(EvReset()); // Should be no-op
}

void test_services_gaps() {
    // Console Class write order
    Console.begin();
    Console.write('A');
    uint8_t buf[] = {'B', 'C'};
    Console.write(buf, 2); // Triggers Line 53 flush

    // Line 114: flush when begun is false
    auto ca = ConsoleTestAccessor::create(Console);
    ca.setBegun(false);
    Console.flush();

    // Line 145, 150-153: XOFF logic
    ca.setBegun(true);
    ca.setXoffSent(false);
    // Fill RX buffer above high water (3/4 of 128 = 48)
    uint8_t fill[100];
    memset(fill, 'X', sizeof(fill));
    Console._push(etl::span<const uint8_t>(fill, 60));
    TEST_ASSERT(ca.getXoffSent() == true);

    // DataStore Class requestGet fail
    Bridge.enterSafeState();
    DataStore.requestGet("key"); // Triggers Line 36-37

    // DataStore Pop empty
    DataStore.reset();
    auto dsa = DataStoreTestAccessor::create(DataStore);
    dsa.popPendingKey(); // Line 43-44
    
    // DataStore track long key
    TEST_ASSERT(dsa.trackPendingKey("this_key_is_way_too_long_for_the_buffer_limit") == false);

    // Process Class runAsync overflow
    Bridge.enterSafeState();
    Process.runAsync("cmd"); // Line 30

    // Process Poll cleanup
    auto ba = TestAccessor::create(Bridge);
    ba.setIdle();
    Process.poll(123);
    
    // Line 53: cleanup if sendPidCommand fails (not idle)
    Bridge.enterSafeState();
    Process.poll(456);

    // Line 75-77: normal pop
    ba.setIdle();
    Process.poll(789);
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
    // Minimum 6 bytes for ProcessPollResponse: status(1), exit(1), out_len(2), err_len(2)
    f.header.payload_length = 6; 
    uint8_t pbuf[6] = {0};
    etl::copy_n(pbuf, 6, f.payload.data());
    CommandContext ctx{&f, f.header.command_id, false, false};
    ba.routeProcessCommand(ctx); 
}

void test_bridge_status_system_gaps() {
    auto ba = TestAccessor::create(Bridge);
    ba.setIdle();
    
    // Line 417: STATUS_MALFORMED
    rpc::Frame f_mal;
    f_mal.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED);
    f_mal.header.payload_length = 2;
    rpc::write_u16_be(f_mal.payload.data(), 0x1234);
    CommandContext ctx_mal{&f_mal, f_mal.header.command_id, false, false};
    ba.routeStatusCommand(ctx_mal);

    // Line 442: CMD_GET_CAPABILITIES
    rpc::Frame f_cap;
    f_cap.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
    f_cap.header.payload_length = 0;
    CommandContext ctx_cap{&f_cap, f_cap.header.command_id, false, false};
    ba.routeSystemCommand(ctx_cap);
}

void test_bridge_handle_dedup_ack_gaps() {
    auto ba = TestAccessor::create(Bridge);
    ba.setIdle();
    
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
    f.header.payload_length = 0;
    f.crc = 0xDEADBEEF;
    
    CommandContext ctx{&f, f.header.command_id, true /* duplicate */, false};
    
    // Line 422: flush_on_duplicate = false
    struct DummyHandler { void operator()() {} } handler;
    ba.handleDedupAck(ctx, handler, false);

    // Line 420: flush_on_duplicate = true
    ba.handleDedupAck(ctx, handler, true);

    // Non-duplicate path
    CommandContext ctx2{&f, f.header.command_id, false /* not duplicate */, false};
    ba.handleDedupAck(ctx2, handler, true);
}

void test_bridge_emit_status_flash() {
    // Line 814-826: Flash variant
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("Flash Error"));
}

void test_bridge_gpio_read_gaps() {
    auto ba = TestAccessor::create(Bridge);
    ba.setIdle();
    
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ);
    f.header.payload_length = 1;
    f.payload[0] = 13;
    
    // Line 701: Duplicate read
    CommandContext ctx{&f, f.header.command_id, true /* duplicate */, false};
    ba.routeGpioCommand(ctx);
    
    // Line 703-710: Normal read
    CommandContext ctx2{&f, f.header.command_id, false, false};
    ba.routeGpioCommand(ctx2);
    
    // Invalid pin
    f.payload[0] = 255;
    ba.routeGpioCommand(ctx2);
}

void test_etl_handle_error_gap() {
    // Line 1223-1226: etl::handle_error
    // We can't easily throw etl::exception without ETL_THROW_EXCEPTIONS
    // but we can call it directly since it's weak in Bridge.cpp
    // and we removed our redefinition.
    // Wait, it is in namespace etl.
}

int main() {
    printf("ARDUINO COVERAGE BOOST TEST START\n");
    Bridge.begin(115200);
    
    test_fsm_only();
    test_rle_gaps();
    test_rpc_structs_gaps();
    test_bridge_writer_gaps();
    test_bridge_core_gaps();
    test_bridge_core_timeout_status();
    test_fsm_gaps_more();
    test_router_gaps();
    test_services_gaps();
    test_bridge_handle_dedup_ack_gaps();
    test_bridge_emit_status_flash();
    test_bridge_gpio_read_gaps();

    printf("ARDUINO COVERAGE BOOST TEST END\n");
    return 0;
}

Stream* g_arduino_stream_delegate = nullptr;
// Redefinition removed to allow covering Bridge.cpp version
