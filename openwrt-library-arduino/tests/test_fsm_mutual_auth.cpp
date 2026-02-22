#include <stdio.h>
#include <stdint.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "security/security.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"
#include "test_support.h"
#include "BridgeTestInterface.h"

static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }
void delay(unsigned long ms) { g_test_millis += ms; }

using namespace rpc;
using namespace bridge;

// Dummy stream for testing
class MockStream : public Stream {
public:
    size_t write(uint8_t) override { return 1; }
    size_t write(const uint8_t* b, size_t s) override { return s; }
    int available() override { return 0; }
    int read() override { return -1; }
    int peek() override { return -1; }
    void flush() override {}
};

MockStream g_test_stream;
Stream* g_arduino_stream_delegate = &g_test_stream;
HardwareSerial Serial;
HardwareSerial Serial1;

// Global Instances
BridgeClass Bridge(g_test_stream);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

void test_fsm_initial_state() {
    BridgeClass localBridge(g_test_stream);
    localBridge.begin(115200);
    TEST_ASSERT(localBridge.getStateId() == bridge::fsm::STATE_UNSYNCHRONIZED);
    printf("  -> Initial state: OK\n");
}

void test_mutual_auth_success() {
    BridgeClass localBridge(g_test_stream);
    const char* secret = "secret_1234567890123456";
    localBridge.begin(115200, secret, 23);
    auto accessor = bridge::test::TestAccessor::create(localBridge);

    // Prepare valid SYNC frame with correct Tag
    uint8_t nonce[16] = {0xAA};
    uint8_t tag[16];
    
    // Internal helper to compute expected tag
    accessor.computeHandshakeTag(nonce, 16, tag);

    rpc::Frame sync_frame;
    sync_frame.header.version = rpc::PROTOCOL_VERSION;
    sync_frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    sync_frame.header.payload_length = 32;
    memcpy(sync_frame.payload.data(), nonce, 16);
    memcpy(sync_frame.payload.data() + 16, tag, 16);

    accessor.dispatch(sync_frame);

    TEST_ASSERT(localBridge.isSynchronized());
    TEST_ASSERT(localBridge.getStateId() == bridge::fsm::STATE_IDLE);
    printf("  -> Mutual Auth Success: OK\n");
}

void test_mutual_auth_failure_wrong_tag() {
    BridgeClass localBridge(g_test_stream);
    const char* secret = "secret_1234567890123456";
    localBridge.begin(115200, secret, 23);
    auto accessor = bridge::test::TestAccessor::create(localBridge);

    uint8_t nonce[16] = {0xAA};
    uint8_t wrong_tag[16] = {0xFF};

    rpc::Frame sync_frame;
    sync_frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    sync_frame.header.payload_length = 32;
    memcpy(sync_frame.payload.data(), nonce, 16);
    memcpy(sync_frame.payload.data() + 16, wrong_tag, 16);

    accessor.dispatch(sync_frame);

    TEST_ASSERT(!localBridge.isSynchronized());
    TEST_ASSERT(localBridge.getStateId() == bridge::fsm::STATE_FAULT);
    printf("  -> Mutual Auth Failure (Wrong Tag): OK\n");
}

void test_mutual_auth_failure_malformed_length() {
    BridgeClass localBridge(g_test_stream);
    localBridge.begin(115200, "secret", 6);
    auto accessor = bridge::test::TestAccessor::create(localBridge);

    rpc::Frame sync_frame;
    sync_frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    sync_frame.header.payload_length = 16; // Too short, expected 32 when secret is set

    accessor.dispatch(sync_frame);

    TEST_ASSERT(localBridge.getStateId() == bridge::fsm::STATE_UNSYNCHRONIZED); // Should just ignore malformed
    printf("  -> Mutual Auth Failure (Malformed Length): OK\n");
}

void test_fsm_transitions_running() {
    BridgeClass localBridge(g_test_stream);
    localBridge.begin(115200);
    auto accessor = bridge::test::TestAccessor::create(localBridge);

    // Sync without secret
    rpc::Frame sync_frame;
    sync_frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    sync_frame.header.payload_length = 16;
    accessor.dispatch(sync_frame);
    TEST_ASSERT(localBridge.isSynchronized());

    // Send a command that requires ACK
    localBridge.sendFrame(rpc::CommandId::CMD_SET_PIN_MODE, nullptr, 0);
    TEST_ASSERT(localBridge.getStateId() == bridge::fsm::STATE_AWAITING_ACK);

    // Receive ACK
    rpc::Frame ack_frame;
    ack_frame.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_ACK);
    ack_frame.header.payload_length = 2;
    rpc::write_u16_be(ack_frame.payload.data(), rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE));
    accessor.dispatch(ack_frame);
    
    TEST_ASSERT(localBridge.getStateId() == bridge::fsm::STATE_IDLE);
    printf("  -> FSM Transitions (Idle -> AwaitingAck -> Idle): OK\n");
}

void test_fsm_timeout_to_unsynchronized() {
    BridgeClass localBridge(g_test_stream);
    localBridge.begin(115200);
    auto accessor = bridge::test::TestAccessor::create(localBridge);

    // Sync
    rpc::Frame sync_frame;
    sync_frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    sync_frame.header.payload_length = 16;
    accessor.dispatch(sync_frame);
    
    // Disable retries for immediate timeout
    accessor.setAckRetryLimit(0);

    // Send command, wait for ACK
    localBridge.sendFrame(rpc::CommandId::CMD_SET_PIN_MODE, nullptr, 0);
    TEST_ASSERT(localBridge.getStateId() == bridge::fsm::STATE_AWAITING_ACK);
    
    // Explicitly trigger ACK timeout via accessor
    accessor.onAckTimeout();

    if (localBridge.getStateId() != bridge::fsm::STATE_UNSYNCHRONIZED) {
        printf("DEBUG: Expected state %d, got %d\n", bridge::fsm::STATE_UNSYNCHRONIZED, localBridge.getStateId());
    }
    TEST_ASSERT(localBridge.getStateId() == bridge::fsm::STATE_UNSYNCHRONIZED);
    printf("  -> FSM Timeout to Unsynchronized: OK\n");
}

int main() {
    printf("FSM & MUTUAL AUTH TEST SUITE\n");
    test_fsm_initial_state();
    test_mutual_auth_success();
    test_mutual_auth_failure_wrong_tag();
    test_mutual_auth_failure_malformed_length();
    test_fsm_transitions_running();
    test_fsm_timeout_to_unsynchronized();
    printf("ALL TESTS PASSED\n");
    return 0;
}
