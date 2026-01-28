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

// Global instances
HardwareSerial Serial;
HardwareSerial Serial1;
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;
BridgeClass Bridge(Serial1);

void test_bridge_gaps() {
    MockStream stream;
    BridgeClass localBridge(stream);
    localBridge.begin(115200);
    
    localBridge.process();
    localBridge.flushStream();
    
    // Inject data to hit flushRx while loop in begin (if we called it again)
    uint8_t dummy_data[] = {1, 2, 3};
    stream.rx_buffer.append(dummy_data, 3);
    // In our new architecture, begin() clears RX.
}

int main() {
    test_bridge_gaps();
    printf("BridgeCore Coverage Gaps Passed\n");
    return 0;
}