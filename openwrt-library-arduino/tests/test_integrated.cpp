#include <stdio.h>
#include <stdint.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "security/security.h"
#include "protocol/rle.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"
#include "test_support.h"
#include "BridgeTestInterface.h"
#include <etl/span.h>

static unsigned long g_test_millis = 0;
unsigned long millis() { 
    return g_test_millis++; 
}

using namespace rpc;
using namespace bridge;

// --- MOCKS ---

class BetterMockStream : public Stream {
public:
    uint8_t rx_buf[1024];
    size_t rx_head = 0;
    size_t rx_tail = 0;

    size_t write(uint8_t) override { return 1; }
    size_t write(const uint8_t* b, size_t s) override { (void)b; return s; }
    
    int available() override { 
        return (rx_tail >= rx_head) ? (rx_tail - rx_head) : 0; 
    }
    
    int read() override { 
        if (available() > 0) return rx_buf[rx_head++];
        return -1;
    }
    
    int peek() override {
        if (available() > 0) return rx_buf[rx_head];
        return -1;
    }
    
    void flush() override {}

    void inject(const uint8_t* b, size_t s) {
        if (rx_tail + s <= sizeof(rx_buf)) {
            memcpy(rx_buf + rx_tail, b, s);
            rx_tail += s;
        }
    }

    void clear() { rx_head = 0; rx_tail = 0; }
};

BetterMockStream g_bridge_stream;
HardwareSerial Serial;
HardwareSerial Serial1;
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
BridgeClass Bridge(g_bridge_stream);

class FullMockStream : public Stream {
public:
    size_t write(uint8_t) override { return 1; }
    size_t write(const uint8_t* b, size_t s) override { (void)b; return s; }
    int available() override { return 0; }
    int read() override { return -1; }
    int peek() override { return -1; }
    void flush() override {}
};

// --- TEST SUITES ---

void integrated_test_rle() {
    uint8_t in[] = "AAAAABBBCCCC";
    uint8_t enc[32], dec[32];
    size_t el = rle::encode(etl::span<const uint8_t>(in, 12), etl::span<uint8_t>(enc, 32));
    size_t dl = rle::decode(etl::span<const uint8_t>(enc, el), etl::span<uint8_t>(dec, 32));
    TEST_ASSERT(dl == 12 && memcmp(in, dec, 12) == 0);
    
    uint8_t in2[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    el = rle::encode(etl::span<const uint8_t>(in2, 5), etl::span<uint8_t>(enc, 32));
    dl = rle::decode(etl::span<const uint8_t>(enc, el), etl::span<uint8_t>(dec, 32));
    TEST_ASSERT(dl == 5 && memcmp(in2, dec, 5) == 0);
}

void integrated_test_protocol() {
    FrameBuilder b;
    FrameParser p;
    uint8_t raw[128];
    uint8_t pl[] = {0x01, 0x02, 0x03};
    size_t rl = b.build(etl::span<uint8_t>(raw, 128), 0x100, etl::span<const uint8_t>(pl, 3));
    auto result = p.parse(etl::span<const uint8_t>(raw, rl));
    TEST_ASSERT(result.has_value());
    Frame f = result.value();
    TEST_ASSERT(f.header.command_id == 0x100);
}

void integrated_test_bridge_core() {
    FullMockStream stream;
    BridgeClass localBridge(stream);
    localBridge.begin(115200, "secret");
    auto accessor = bridge::test::TestAccessor::create(localBridge);
    
    rpc::Frame sync;
    sync.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    sync.header.payload_length = 32; // 16 nonce + 16 tag
    uint8_t nonce[16];
    etl::fill_n(nonce, 16, uint8_t{0xAA});
    memcpy(sync.payload.data(), nonce, 16);
    
    uint8_t tag[16];
    accessor.computeHandshakeTag(nonce, 16, tag);
    memcpy(sync.payload.data() + 16, tag, 16);

    accessor.dispatch(sync);
    TEST_ASSERT(localBridge.isSynchronized());
    
    rpc::Frame gpio;
    gpio.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
    gpio.header.payload_length = 2;
    gpio.payload[0] = 13; gpio.payload[1] = 1;
    accessor.dispatch(gpio);
    
    localBridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, (const uint8_t*)"X", 1);
    accessor.retransmitLastFrame();
}

void integrated_test_components() {
    Console.begin();
    Console.write((uint8_t)'t');
    Console.flush();
    
    #if BRIDGE_ENABLE_DATASTORE
    DataStore.put("k", "v");
    #endif
    #if BRIDGE_ENABLE_MAILBOX
    Mailbox.send("m");
    #endif
    #if BRIDGE_ENABLE_FILESYSTEM
    FileSystem.read("f");
    #endif
    #if BRIDGE_ENABLE_PROCESS
    Process.run("ls");
    #endif
}

void integrated_test_error_branches() {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, "err");
    Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    Bridge.enterSafeState();
    TEST_ASSERT(rpc::security::run_cryptographic_self_tests());
}

// Static callbacks for extreme coverage test
static void test_ds_cb(etl::string_view k, etl::span<const uint8_t> v) { (void)k; (void)v; }
static void test_fs_cb(const uint8_t* d, uint16_t l) { (void)d; (void)l; }
static void test_pr_run_cb(rpc::StatusCode s, const uint8_t* out, uint16_t ol, const uint8_t* err, uint16_t el) { 
    (void)s; (void)out; (void)ol; (void)err; (void)el; 
}
static void test_pr_poll_cb(rpc::StatusCode s, uint8_t ec, const uint8_t* out, uint16_t ol, const uint8_t* err, uint16_t el) {
    (void)s; (void)ec; (void)out; (void)ol; (void)err; (void)el;
}
static void test_pr_async_cb(int16_t p) { (void)p; }
static void test_mb_cb(const uint8_t* m, uint16_t l) { (void)m; (void)l; }
static void test_mb_avail_cb(uint16_t c) { (void)c; }
static void test_dig_cb(uint8_t v) { (void)v; }
static void test_ana_cb(uint16_t v) { (void)v; }
static void test_mem_cb(uint16_t v) { (void)v; }
static void test_status_cb(rpc::StatusCode s, const uint8_t* p, uint16_t l) { (void)s; (void)p; (void)l; }

void integrated_test_extreme_coverage() {
    auto accessor = bridge::test::TestAccessor::create(Bridge);

    // ... (rest of function omitted for brevity) ...

    // 20. Callbacks
    #if BRIDGE_ENABLE_DATASTORE
    DataStore.onDataStoreGetResponse(DataStoreClass::DataStoreGetHandler::create<test_ds_cb>());
    #endif
    #if BRIDGE_ENABLE_FILESYSTEM
    FileSystem.onFileSystemReadResponse(FileSystemClass::FileSystemReadHandler::create<test_fs_cb>());
    #endif
    #if BRIDGE_ENABLE_PROCESS
    Process.onProcessRunResponse(ProcessClass::ProcessRunHandler::create<test_pr_run_cb>());
    Process.onProcessPollResponse(ProcessClass::ProcessPollHandler::create<test_pr_poll_cb>());
    Process.onProcessRunAsyncResponse(ProcessClass::ProcessRunAsyncHandler::create<test_pr_async_cb>());
    #endif
    #if BRIDGE_ENABLE_MAILBOX
    Mailbox.onMailboxMessage(MailboxClass::MailboxHandler::create<test_mb_cb>());
    Mailbox.onMailboxAvailableResponse(MailboxClass::MailboxAvailableHandler::create<test_mb_avail_cb>());
    #endif
}

int main() {
    printf("INTEGRATED ARDUINO TEST START\n"); fflush(stdout);
    Bridge.begin(115200);
    bridge::test::TestAccessor::create(Bridge).setIdle();

    printf("Running: integrated_test_rle\n"); integrated_test_rle();
    printf("Running: integrated_test_protocol\n"); integrated_test_protocol();
    printf("Running: integrated_test_bridge_core\n"); integrated_test_bridge_core();
    printf("Running: integrated_test_components\n"); integrated_test_components();
    printf("Running: integrated_test_error_branches\n"); integrated_test_error_branches();
    printf("Running: integrated_test_extreme_coverage\n"); integrated_test_extreme_coverage();
    
    printf("INTEGRATED ARDUINO TEST END\n"); fflush(stdout);
    return 0;
}

Stream* g_arduino_stream_delegate = nullptr;
