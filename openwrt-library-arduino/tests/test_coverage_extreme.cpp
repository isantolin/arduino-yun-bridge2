#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"
#include "BridgeTestInterface.h"

using namespace bridge;

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
};

// Global instances required by Bridge.cpp linkage
HardwareSerial Serial;
HardwareSerial Serial1;
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;
BridgeClass Bridge(Serial1);

void test_bridge_core_branches() {
    Bridge.begin(115200);
    Bridge.process();
    Bridge.flushStream();
    
    auto accessor = bridge::test::TestAccessor::create(Bridge);
    uint8_t pl = 0;
    Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, &pl, 1);
    accessor.retransmitLastFrame();
}

void test_on_packet_received_edge_cases() {
    // Bridge already sets itself as instance in constructor/begin
    BridgeClass::onPacketReceived(nullptr, 0);
}

void test_retransmit_empty() {
    BridgeClass localBridge(Serial);
    localBridge.begin(115200);
    auto accessor = bridge::test::TestAccessor::create(localBridge);
    // Should not crash, just do nothing
    accessor.retransmitLastFrame();
}

int main() {
    test_bridge_core_branches();
    test_on_packet_received_edge_cases();
    test_retransmit_empty();
    printf("BridgeCore Coverage Extreme Passed\n");
    return 0;
}