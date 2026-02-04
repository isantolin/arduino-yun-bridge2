#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1
static unsigned long g_test_millis = 10000; // Start at non-zero
unsigned long millis() { return g_test_millis++; }

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_constants.h"
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
    Bridge.begin(115200);
    Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete();
}

// --- COBERTURA BRIDGE.CPP ---
void test_bridge_gaps() {
    CaptureStream stream;
    setup_env(stream);

    rpc::Frame f;
    
    // Gap: _handleSystemCommand default case
    f.header.command_id = 0x4F; 
    Bridge._handleSystemCommand(f);

    // Gap: _handleGpioCommand default case
    f.header.command_id = 0x5F;
    f.header.payload_length = 1;
    f.payload[0] = 13;
    Bridge._handleGpioCommand(f);

    // Gap: dispatch unexpected status codes
    f.header.command_id = 0x3F; // STATUS_CODE_MAX
    Bridge.dispatch(f);

    // Gap: dispatch with compressed flag but decode failure (short payload)
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE) | rpc::RPC_CMD_FLAG_COMPRESSED;
    f.header.payload_length = 1;
    f.payload[0] = 0xFF; // RLE escape sin datos
    Bridge.dispatch(f);

    // Gap: _isRecentDuplicateRx branches
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    f.header.payload_length = 2;
    f.payload[0] = 13; f.payload[1] = 1;
    f.crc = 0x12345678; // Dummy CRC
    Bridge._ack_timeout_ms = 1000; 
    Bridge._ack_retry_limit = 3;
    Bridge._markRxProcessed(f);
    g_test_millis += 1500; // Move into the retry window (elapsed > ack_timeout)
    assert(Bridge._isRecentDuplicateRx(f));

    // Gap: enterSafeState reset logic
    Bridge.enterSafeState();
    assert(!Bridge.isSynchronized());

    // Gap: _handleSystemCommand CMD_LINK_SYNC without secret
    Bridge._shared_secret.clear();
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    f.header.payload_length = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
    etl::fill_n(f.payload.data(), rpc::RPC_HANDSHAKE_NONCE_LENGTH, uint8_t{0xA});
    Bridge._handleSystemCommand(f);

    // Gap: onPacketReceived with various errors
    uint8_t crc_err[] = {0x02, 0x00, 0x00, 0x40, 0x00, 0xDE, 0xAD, 0xBE, 0xEF}; 
    Bridge.onPacketReceived(crc_err, sizeof(crc_err));
    Bridge.process(); 

    // Gap: Retransmission logic and failure streak
    // Note: _onAckTimeout() checks _retry_count >= _ack_retry_limit to enter safe state.
    // _retransmitLastFrame() increments _retry_count but only if _pending_tx_queue is non-empty.
    // For coverage, we directly set _retry_count to exceed limit and call _onAckTimeout.
    Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete(); Bridge._fsm.sendCritical();
    Bridge._ack_timeout_ms = 1000;
    Bridge._ack_retry_limit = 1;
    Bridge._retry_count = Bridge._ack_retry_limit; // Set at limit
    
    // Directly call timeout handler - exceeds limit -> enterSafeState
    Bridge._onAckTimeout();
    assert(!Bridge.isSynchronized());
}

// --- COBERTURA DATASTORE LÍMITES ---
void test_datastore_gaps() {
    CaptureStream stream;
    setup_env(stream);

    // Gap: _trackPendingDatastoreKey overflow
    for(int i=0; i<BRIDGE_MAX_PENDING_DATASTORE + 1; ++i) {
        DataStore.requestGet("key");
    }
}

// --- COBERTURA CONSOLE.CPP ---
void test_console_gaps() {
    CaptureStream stream;
    setup_env(stream);
    Console.begin();

    // Gap: write(buffer, size) chunking
    uint8_t large_buf[rpc::MAX_PAYLOAD_SIZE + 10];
    etl::fill_n(large_buf, sizeof(large_buf), uint8_t{'A'});
    Console.write(large_buf, sizeof(large_buf));

    // Gap: read() high/low watermarks
    for(int i=0; i<BRIDGE_CONSOLE_RX_BUFFER_SIZE; ++i) Console._rx_buffer.push(i);
    Console._xoff_sent = true;
    while(!Console._rx_buffer.empty()) Console.read();
    assert(!Console._xoff_sent);

    // Gap: flush() with empty buffer
    Console._tx_buffer.clear();
    Console.flush();
}

// --- COBERTURA FILESYSTEM.CPP ---
void test_filesystem_gaps() {
    CaptureStream stream;
    setup_env(stream);

    // Gap: write with data too large
    uint8_t super_large[rpc::MAX_PAYLOAD_SIZE + 10];
    FileSystem.write("test.txt", super_large, sizeof(super_large));

    // Gap: read() with invalid path
    FileSystem.read(nullptr);
    FileSystem.read("");
    char long_path[rpc::RPC_MAX_FILEPATH_LENGTH + 5];
    etl::fill_n(long_path, sizeof(long_path), 'p');
    long_path[sizeof(long_path)-1] = '\0';
    FileSystem.read(long_path);

    // Gap: remove with overflowed path
    FileSystem.remove(long_path);

    // Gap: handleResponse with valid read handler
    FileSystem.onFileSystemReadResponse([](const uint8_t* d, uint16_t s) { (void)d; (void)s; });
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
    f.header.payload_length = 4;
    memcpy(f.payload.data(), "DATA", 4);
    FileSystem.handleResponse(f);
}

// --- COBERTURA MAILBOX.CPP ---
void test_mailbox_gaps() {
    CaptureStream stream;
    setup_env(stream);

    // Gap: requestRead, requestAvailable
    Mailbox.requestRead();
    Mailbox.requestAvailable();

    // Gap: handleResponse CMD_MAILBOX_AVAILABLE_RESP
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
    f.header.payload_length = 2;
    rpc::write_u16_be(f.payload.data(), 5);
    Mailbox.handleResponse(f);

    // Gap: handleResponse with other command
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    Mailbox.handleResponse(f);
}

// --- COBERTURA PROCESS.CPP ---
void test_process_gaps() {
    CaptureStream stream;
    setup_env(stream);

    // Gap: poll with PID tracking
    Process.runAsync("test");
    // Simulamos que el Bridge recibió el PID 42
    rpc::Frame f_pid;
    f_pid.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
    f_pid.header.payload_length = 2;
    rpc::write_u16_be(f_pid.payload.data(), 42);
    Process.handleResponse(f_pid);
    Process.poll(42);
    Process.kill(42);

    // Gap: handleResponse CMD_PROCESS_POLL_RESP (not running)
    rpc::Frame f;
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
    f.header.payload_length = 7;
    f.payload[0] = 0x30; // OK
    f.payload[1] = 0; // Not Running
    rpc::write_u16_be(&f.payload[2], 1); 
    f.payload[4] = 'o';
    rpc::write_u16_be(&f.payload[5], 1); 
    f.payload[7] = 'e';
    Process.handleResponse(f);
}

} // namespace

int main() {
    printf("EXTREME ARDUINO COVERAGE V2 START\n");
    test_bridge_gaps();
    test_datastore_gaps();
    test_console_gaps();
    test_filesystem_gaps();
    test_mailbox_gaps();
    test_process_gaps();
    printf("EXTREME ARDUINO COVERAGE V2 END\n");
    return 0;
}