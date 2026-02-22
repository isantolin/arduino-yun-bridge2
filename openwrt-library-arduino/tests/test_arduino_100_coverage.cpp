#include <stdio.h>
#include <stdint.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rle.h"
#include "security/security.h"
#include "test_support.h"

// Stubs Globales
HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

namespace {

void setup_coverage_env() {
    Bridge.begin();
    auto ba = bridge::test::TestAccessor::create(Bridge);
    ba.setIdle();
}

void test_rpc_frame_gaps() {
    rpc::FrameBuilder builder;
    uint8_t buffer[rpc::MAX_RAW_FRAME_SIZE];
    uint8_t payload[] = {1, 2, 3};
    
    // Coverage for FrameBuilder::build with payload
    builder.build(etl::span<uint8_t>(buffer), 0x100, etl::span<const uint8_t>(payload, 3));
    
    // Coverage for FrameParser::parse errors (already covered but for good measure)
    rpc::FrameParser parser;
    uint8_t short_buf[] = {0x02, 0x00};
    auto res = parser.parse(etl::span<const uint8_t>(short_buf, 2));
    TEST_ASSERT(!res.has_value());
}

void test_bridge_extra_gaps() {
    auto ba = bridge::test::TestAccessor::create(Bridge);
    rpc::Frame f;
    
    // Gap: _handleSystemCommand CMD_GET_VERSION with non-zero length (should ignore)
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
    f.header.payload_length = 1;
    ba.handleSystemCommand(f);

    // Gap: _handleSystemCommand CMD_GET_FREE_MEMORY with non-zero length
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY);
    f.header.payload_length = 1;
    ba.handleSystemCommand(f);

    // Gap: _handleSystemCommand CMD_GET_CAPABILITIES with non-zero length
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
    f.header.payload_length = 1;
    ba.handleSystemCommand(f);

    // Gap: onUnknownCommand without handler (emits STATUS_CMD_UNKNOWN)
    f.header.command_id = 0xAA; // Arbitrary unknown
    ba.routeUnknownCommand(bridge::router::CommandContext{&f, 0xAA, false, false});
}

void test_console_extra_gaps() {
    auto ca = bridge::test::ConsoleTestAccessor::create(Console);
    ca.setBegun(true);
    
    // Gap: write(c) when buffer full and flush fails
    // Force unsynchronized to make flush fail
    auto ba = bridge::test::TestAccessor::create(Bridge);
    ba.setUnsynchronized();
    
    // Clear buffer first to ensure we know the state
    ca.clearTxBuffer();
    
    // Fill buffer to capacity
    while (!ca.isTxBufferFull()) {
        ca.pushTxByte('A');
    }
    
    // This write should attempt flush, flush fails due to unsync, 
    // buffer remains full, write returns 0.
    TEST_ASSERT_EQ_UINT(Console.write('B'), 0);

    // Gap: available(), peek(), read() coverage
    ca.pushRxByte('X');
    TEST_ASSERT(Console.available() > 0);
    TEST_ASSERT(Console.peek() == 'X');
    TEST_ASSERT(Console.read() == 'X');
}

void test_datastore_extra_gaps() {
    auto ba = bridge::test::TestAccessor::create(Bridge);
    rpc::Frame f;
    
    // Gap: handleResponse CMD_DATASTORE_GET_RESP without handler
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
    f.header.payload_length = 2;
    f.payload[0] = 1; f.payload[1] = 'V';
    ba.dispatch(f);

    // Gap: handleResponse with other command
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    ba.dispatch(f);
}

void test_mailbox_extra_gaps() {
    auto ba = bridge::test::TestAccessor::create(Bridge);
    rpc::Frame f;
    
    // Gap: handleResponse CMD_MAILBOX_READ_RESP without handler
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
    f.header.payload_length = 3;
    rpc::write_u16_be(f.payload.data(), 1); f.payload[2] = 'M';
    ba.dispatch(f);

    // Gap: handleResponse CMD_MAILBOX_AVAILABLE_RESP without handler
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
    f.header.payload_length = 2;
    rpc::write_u16_be(f.payload.data(), 10);
    ba.dispatch(f);
}

void test_process_extra_gaps() {
    auto ba = bridge::test::TestAccessor::create(Bridge);
    rpc::Frame f;
    
    // Gap: handleResponse CMD_PROCESS_RUN_RESP without handler
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_RESP);
    f.header.payload_length = 6;
    f.payload[0] = 0x30;
    ba.dispatch(f);

    // Gap: handleResponse CMD_PROCESS_RUN_ASYNC_RESP with handler
    Process.onProcessRunAsyncResponse(BridgeClass::ProcessRunAsyncHandler::create([](int16_t p){(void)p;}));
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
    f.header.payload_length = 2;
    rpc::write_u16_be(f.payload.data(), 456);
    ba.dispatch(f);

    // Gap: handleResponse CMD_PROCESS_POLL_RESP without handler
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
    f.header.payload_length = 6;
    ba.dispatch(f);

    // Gap: handleResponse with other command
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    ba.dispatch(f);
}

void test_rle_gaps() {
    uint8_t src[10];
    uint8_t dst[10];
    // Gap: should_compress with small buffer
    TEST_ASSERT(!rle::should_compress(etl::span<const uint8_t>(src, 2)));
    // Gap: encode/decode empty/zero
    TEST_ASSERT_EQ_UINT(rle::encode(etl::span<const uint8_t>(src, 0), etl::span<uint8_t>(dst, 10)), 0);
    TEST_ASSERT_EQ_UINT(rle::decode(etl::span<const uint8_t>(src, 0), etl::span<uint8_t>(dst, 10)), 0);
}

void test_security_gaps() {
    // Kat failure simulation is hard without mocks, but we cover the self tests
    TEST_ASSERT(rpc::security::run_cryptographic_self_tests());
}

} // namespace

int main() {
    printf("ARDUINO 100%% COVERAGE TEST START\n");
    setup_coverage_env();
    
    printf("  -> Testing rpc_frame_gaps\n"); test_rpc_frame_gaps();
    printf("  -> Testing bridge_extra_gaps\n"); test_bridge_extra_gaps();
    printf("  -> Testing console_extra_gaps\n"); test_console_extra_gaps();
    printf("  -> Testing test_datastore_extra_gaps\n"); test_datastore_extra_gaps();
    printf("  -> Testing mailbox_extra_gaps\n"); test_mailbox_extra_gaps();
    printf("  -> Testing process_extra_gaps\n"); test_process_extra_gaps();
    printf("  -> Testing rle_gaps\n"); test_rle_gaps();
    printf("  -> Testing security_gaps\n"); test_security_gaps();

    printf("ARDUINO 100%% COVERAGE TEST END\n");
    return 0;
}