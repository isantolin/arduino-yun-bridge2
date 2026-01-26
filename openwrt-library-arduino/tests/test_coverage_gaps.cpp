#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "arduino/BridgeTransport.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

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

namespace bridge {
namespace test {
class TestAccessor {
public:
    static void setInstance(BridgeTransport* instance) {
        BridgeTransport::_instance = instance;
    }
};
} // namespace test
} // namespace bridge

void test_hardware_serial_null_paths() {
    MockStream stream;
    BridgeTransport transport(stream);
    
    transport.begin(115200);
    transport.setBaudrate(9600);
    transport.flush();
    transport.end();
    
    // Inject data to hit flushRx while loop
    uint8_t dummy_data[] = {1, 2, 3};
    stream.rx_buffer.append(dummy_data, 3);
    transport.flushRx();
    
    uint8_t pl = 0;
    transport.sendFrame(rpc::to_underlying(rpc::StatusCode::STATUS_OK), &pl, 1);
    transport.sendControlFrame(rpc::to_underlying(rpc::StatusCode::STATUS_OK));
    transport.retransmitLastFrame();
}

void test_on_packet_received_no_instance() {
    bridge::test::TestAccessor::setInstance(nullptr);
    BridgeTransport::onPacketReceived(nullptr, 0);
}

void test_retransmit_empty_buffer() {
    MockStream stream;
    BridgeTransport transport(stream);
    transport.begin(115200);
    if (transport.retransmitLastFrame()) {
        exit(1);
    }
}

int main() {
    test_hardware_serial_null_paths();
    test_on_packet_received_no_instance();
    test_retransmit_empty_buffer();
    printf("BridgeTransport Coverage Gaps Test Passed\n");
    return 0;
}
