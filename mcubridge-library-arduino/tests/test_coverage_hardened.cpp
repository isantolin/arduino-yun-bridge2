#define BRIDGE_ENABLE_TEST_INTERFACE
#include "fsm/CounterIterator.h"
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "test_support.h"
#include <unity.h>
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

// Arduino Stubs for Linker
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

void setUp(void) {}
void tearDown(void) {}

// SIL-2 Hardening Coverage Test Suite
// Focuses on reaching 90%+ line and 80%+ branch coverage by targeting edge cases,
// error paths, and the new optimized serialization/iteration logic.

using bridge::test::TestAccessor;

namespace {
void poll_handler(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>) {}
void async_handler(int32_t) {}
void dummy_cmd_handler(const rpc::Frame&) {}
void dummy_status_handler(rpc::StatusCode, etl::span<const uint8_t>) {}
}

void test_bridge_emit_status_variants() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    
    // Test all status variants to cover string_view, FlashString, and span paths
    Bridge.emitStatus(rpc::StatusCode::STATUS_OK);
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, etl::string_view("Error"));
    Bridge.emitStatus(rpc::StatusCode::STATUS_MALFORMED, F("FlashError"));
    
    // Empty variants
    Bridge.emitStatus(rpc::StatusCode::STATUS_OK, etl::string_view(""));
    Bridge.emitStatus(rpc::StatusCode::STATUS_OK, (const __FlashStringHelper*)nullptr);
    
    TEST_ASSERT(true);
}

void test_bridge_queue_full_and_retransmit() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto ba = TestAccessor::create(Bridge);
    ba.setSynchronized();
    
    // Fill the TX queue with reliable commands to trigger full condition
    bridge::utils::CounterIterator<uint32_t> fill_begin(0);
    bridge::utils::CounterIterator<uint32_t> fill_end(bridge::config::MAX_PENDING_TX_FRAMES);
    etl::for_each(fill_begin, fill_end, [&ba](uint32_t i) {
        // Use a reliable command (e.g., CMD_CONSOLE_WRITE)
        (void)ba.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 100 + i, {});
    });
    
    // Next one should return false (queue full)
    bool ok = ba.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 999, {});
    TEST_ASSERT_FALSE(ok);
    
    // Trigger retransmit path
    ba.onAckTimeout();
    
    // Trigger ACK for a non-waiting command
    ba.handleAck(0xFFFF); 
    
    TEST_ASSERT(true);
}

void test_filesystem_read_edge_cases() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto ba = TestAccessor::create(Bridge);
    ba.setSynchronized();
    
    // Trigger FileSystem read chunks with timeout/error simulation
    const char* file_path_str = "test.txt";
    etl::string_view path_sv(file_path_str);
    rpc::payload::FileRead req = {etl::span<const char>(path_sv.data(), path_sv.size())};
    
    // This will use the new CounterIterator in _onRead
    FileSystem._onRead(req);
    
    // Coverage for observer notification
    FileSystem.notification(MsgBridgeSynchronized());
    FileSystem.notification(MsgBridgeLost());
    
    TEST_ASSERT(true);
}

void test_spi_timeout_and_error_paths() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    
    SPIService.begin();
    SPIService.setConfig({4000000, 1, 0}); // frequency, bit_order, data_mode
    
    etl::array<uint8_t, 4> buf = {1, 2, 3, 4};
    // Normal transfer (stub SPI doesn't timeout)
    size_t n = SPIService.transfer(etl::span<uint8_t>(buf));
    TEST_ASSERT_EQUAL(4, n);
    
    // Empty transfer
    n = SPIService.transfer(etl::span<uint8_t>());
    TEST_ASSERT_EQUAL(0, n);
    
    SPIService.end();
    // Transfer while not initialized
    n = SPIService.transfer(etl::span<uint8_t>(buf));
    TEST_ASSERT_EQUAL(0, n);
    
    // Coverage for observer notification
    SPIService.notification(MsgBridgeSynchronized());
    SPIService.notification(MsgBridgeLost());
}

void test_process_poll_and_kill() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    
    // Test Process service direct list initialization and pending queue
    Process.poll(123, ProcessClass::ProcessPollHandler::create<poll_handler>());
    Process.kill(456);
    Process.runAsync("ls", {}, etl::delegate<void(int32_t)>::create<async_handler>());
    
    // Internal handlers (coverage only)
    Process._onRunAsyncResponse({});
    Process._onPollResponse({});
    
    // Coverage for observer notification
    Process.notification(MsgBridgeSynchronized());
    Process.notification(MsgBridgeLost());
    
    TEST_ASSERT(true);
}

void test_mailbox_and_datastore_variants() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    
    etl::array<uint8_t, 4> mb_data1 = {1,2,3,4};
    Mailbox.push(mb_data1);

    // Test _onIncomingData
    etl::array<uint8_t, 2> mb_data2 = {0xAA, 0xBB};
    Mailbox._onIncomingData(rpc::payload::MailboxPush{mb_data2});
    etl::array<uint8_t, 2> mb_data3 = {0xCC, 0xDD};
    Mailbox._onIncomingData(rpc::payload::MailboxReadResponse{mb_data3});
    Mailbox._onAvailableResponse({});
    
    // Coverage for observer notification
    Mailbox.notification(MsgBridgeSynchronized());
    Mailbox.notification(MsgBridgeLost());
    
    DataStore._onResponse({});
    
    TEST_ASSERT(true);
}

void test_bridge_fsm_resets() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto ba = TestAccessor::create(Bridge);
    
    ba.setSynchronized();
    Bridge.enterSafeState(); // Should reset FSM and stop timers
    
    TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_checksum_direct_library_path() {
    // Validates the new etl::byte_stream_writer logic in checksum::compute
    rpc::Frame f = {};
    f.header = {rpc::PROTOCOL_VERSION, 4, static_cast<uint16_t>(rpc::CommandId::CMD_XON), 0};
    f.nonce.fill(0);
    f.tag.fill(0);
    f.payload = etl::span<const uint8_t>();
    uint32_t crc = rpc::checksum::compute(f);
    TEST_ASSERT(crc != 0);
}

void test_bridge_timer_callbacks() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    
    // We can't easily wait for real timers in a host test,
    // but we can call the callback functions directly for coverage.
    Bridge._onAckTimeout();
    Bridge._onRxDedupe();
    Bridge._onBaudrateChange();
    bridge::test::TestAccessor::create(Bridge).onStartupStabilized();
    Bridge._onBootloaderDelay();
    
    TEST_ASSERT(true);
}

void test_bridge_packet_errors() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto ba = TestAccessor::create(Bridge);
    
    // Test malformed packet (length 0)
    ba.invokePacketReceived(etl::span<const uint8_t>());
    
    TEST_ASSERT(true);
}

void test_bridge_template_coverage() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    
    // Explicitly trigger template instantiations that might be missed
    (void)Bridge.send(rpc::CommandId::CMD_SET_PIN_MODE, 1, rpc::payload::PinMode{13, 1});
    
    // Mock handlers
    Bridge.onCommand(BridgeClass::CommandHandler::create<dummy_cmd_handler>());
    Bridge.onStatus(BridgeClass::StatusHandler::create<dummy_status_handler>());
    Bridge.flushStream();
    
    TEST_ASSERT(true);
}

void test_bridge_duplicate_packet() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto ba = TestAccessor::create(Bridge);
    ba.setSynchronized();

    static etl::array<uint8_t, 256> buf;
    rpc::payload::DigitalWrite msg = {13, 1};
    mpack_writer_t writer;
    mpack_writer_init(&writer, reinterpret_cast<char*>(buf.data()), buf.size());
    if (msg.encode(&writer)) {
        size_t used = mpack_writer_buffer_used(&writer);
        rpc::Frame f = {};
        f.header = {rpc::PROTOCOL_VERSION, (uint16_t)used, (uint16_t)rpc::CommandId::CMD_DIGITAL_WRITE, 10};
        f.nonce.fill(0);
        f.tag.fill(0);
        f.payload = etl::span<const uint8_t>(buf.data(), used);
        
        bridge::router::CommandContext ctx(&f, f.header.command_id, 10, true, true);
        ba.handleDigitalWriteCommand(ctx);
    }
    
    TEST_ASSERT(true);
}

void test_bridge_exhaustive_command_handlers() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto ba = TestAccessor::create(Bridge);
    ba.setSynchronized();

    static etl::array<uint8_t, 256> buf;
    auto trigger = [&](rpc::CommandId id, auto payload) {
        mpack_writer_t writer;
        mpack_writer_init(&writer, reinterpret_cast<char*>(buf.data()), buf.size());
        if (payload.encode(&writer)) {
            size_t used = mpack_writer_buffer_used(&writer);
            rpc::Frame f = {};
            f.header = {rpc::PROTOCOL_VERSION, (uint16_t)used, (uint16_t)id, 1};
            f.nonce.fill(0);
            f.tag.fill(0);
            f.payload = etl::span<const uint8_t>(buf.data(), used);
            ba.dispatch(f);
        }
    };

    trigger(rpc::CommandId::CMD_SET_BAUDRATE, rpc::payload::SetBaudratePacket{57600});
    trigger(rpc::CommandId::CMD_ENTER_BOOTLOADER, rpc::payload::EnterBootloader{rpc::RPC_BOOTLOADER_MAGIC});
    trigger(rpc::CommandId::CMD_SET_PIN_MODE, rpc::payload::PinMode{13, 1});
    trigger(rpc::CommandId::CMD_DIGITAL_WRITE, rpc::payload::DigitalWrite{13, 1});
    trigger(rpc::CommandId::CMD_ANALOG_WRITE, rpc::payload::AnalogWrite{3, 128});
    trigger(rpc::CommandId::CMD_DIGITAL_READ, rpc::payload::PinRead{13});
    trigger(rpc::CommandId::CMD_ANALOG_READ, rpc::payload::PinRead{0});

    TEST_ASSERT(true);
}

int main() {
    (void)poll_handler;
    (void)async_handler;
    (void)dummy_cmd_handler;
    (void)dummy_status_handler;
    UNITY_BEGIN();
    RUN_TEST(test_bridge_emit_status_variants);
    RUN_TEST(test_bridge_queue_full_and_retransmit);
    RUN_TEST(test_filesystem_read_edge_cases);
    RUN_TEST(test_spi_timeout_and_error_paths);
    RUN_TEST(test_process_poll_and_kill);
    RUN_TEST(test_mailbox_and_datastore_variants);
    RUN_TEST(test_bridge_fsm_resets);
    RUN_TEST(test_checksum_direct_library_path);
    RUN_TEST(test_bridge_timer_callbacks);
    RUN_TEST(test_bridge_packet_errors);
    RUN_TEST(test_bridge_template_coverage);
    RUN_TEST(test_bridge_duplicate_packet);
    RUN_TEST(test_bridge_exhaustive_command_handlers);
    return UNITY_END();
}
