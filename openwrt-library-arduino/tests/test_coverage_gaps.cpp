#include "BridgeTestInterface.h"
#include "arduino/BridgeTransport.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

using namespace bridge;

// Local instances for linkage
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