#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

// Services
#include "services/Console.h"
#include "services/FileSystem.h"
#include "services/Process.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/SPIService.h"

unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }
void delay(unsigned long ms) { g_test_millis += ms; }

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using bridge::test::TestAccessor;

void ds_handler(etl::string_view, etl::span<const uint8_t>) {}
void proc_handler(int32_t) {}
void poll_handler(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>) {}
void fs_handler(etl::span<const uint8_t>) {}

void test_bridge_reset_state() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  TEST_ASSERT(ba.isUnsynchronized());
  Bridge.enterSafeState();
  TEST_ASSERT(ba.getStartupStabilizing());
}

void test_bridge_is_recent_duplicate_edge_cases() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  rpc::Frame f = {};
  f.header.sequence_id = 42;
  ba.clearRxHistory();
  TEST_ASSERT(!ba.isRecentDuplicateRx(f));
  ba.markRxProcessed(f);
  TEST_ASSERT(ba.isRecentDuplicateRx(f));
  rpc::Frame f2 = {};
  f2.header.sequence_id = 43;
  TEST_ASSERT(!ba.isRecentDuplicateRx(f2));
  for (int i = 0; i < 30; i++) {
    rpc::Frame temp = {};
    temp.header.sequence_id = static_cast<uint16_t>(100 + i);
    ba.markRxProcessed(temp);
  }
  TEST_ASSERT(!ba.isRecentDuplicateRx(f));
}

void test_bridge_ack_timeout_retry() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto& ba = TestAccessor::create(Bridge);
    ba.setSynchronized();
    rpc::payload::LinkSync ls = {};
    Bridge.send(rpc::CommandId::CMD_LINK_SYNC, 0, ls);
    for(int i=0; i < 20; ++i) ba.onAckTimeout();
}

void test_bridge_dispatch_exhaustive() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto& ba = TestAccessor::create(Bridge);
    ba.setSynchronized();
    
    static uint8_t buf[256];
    auto send_cmd = [&](rpc::CommandId id, auto payload) {
        memset(buf, 0, sizeof(buf));
        msgpack::Encoder enc(buf, sizeof(buf));
        payload.encode(enc);
        rpc::Frame f = {};
        f.header.command_id = (uint16_t)id;
        f.payload = enc.result();
        f.header.payload_length = (uint16_t)f.payload.size();
        ba.dispatch(f);
    };

    auto send_cmd_no_pl = [&](rpc::CommandId id) {
        rpc::Frame f = {};
        f.header.command_id = (uint16_t)id;
        ba.dispatch(f);
    };

    send_cmd_no_pl(rpc::CommandId::CMD_LINK_SYNC);
    send_cmd_no_pl(rpc::CommandId::CMD_GET_VERSION);
    send_cmd_no_pl(rpc::CommandId::CMD_GET_CAPABILITIES);
    send_cmd_no_pl(rpc::CommandId::CMD_LINK_RESET);
    send_cmd_no_pl(rpc::CommandId::CMD_GET_FREE_MEMORY);
    
    send_cmd(rpc::CommandId::CMD_SET_BAUDRATE, rpc::payload::SetBaudratePacket{57600});
    send_cmd(rpc::CommandId::CMD_ENTER_BOOTLOADER, rpc::payload::EnterBootloader{rpc::RPC_BOOTLOADER_MAGIC});
    send_cmd(rpc::CommandId::CMD_SET_PIN_MODE, rpc::payload::PinMode{13, 1});
    send_cmd(rpc::CommandId::CMD_DIGITAL_WRITE, rpc::payload::DigitalWrite{13, 1});
    send_cmd(rpc::CommandId::CMD_ANALOG_WRITE, rpc::payload::AnalogWrite{5, 128});
    send_cmd(rpc::CommandId::CMD_DIGITAL_READ, rpc::payload::PinRead{13});
    send_cmd(rpc::CommandId::CMD_ANALOG_READ, rpc::payload::PinRead{0});
    
    uint8_t data[] = "data";
    rpc::payload::ConsoleWrite cw; cw.data = etl::span<const uint8_t>(data, 4);
    send_cmd(rpc::CommandId::CMD_CONSOLE_WRITE, cw);

    rpc::payload::DatastorePut dp; dp.key = "k"; dp.value = etl::span<const uint8_t>(data, 4);
    send_cmd(rpc::CommandId::CMD_DATASTORE_PUT, dp);

    rpc::payload::DatastoreGet dg; dg.key = "k";
    send_cmd(rpc::CommandId::CMD_DATASTORE_GET, dg);

    rpc::payload::MailboxPush mbp; mbp.data = etl::span<const uint8_t>(data, 4);
    send_cmd(rpc::CommandId::CMD_MAILBOX_PUSH, mbp);

    send_cmd(rpc::CommandId::CMD_MAILBOX_PROCESSED, rpc::payload::MailboxProcessed{1});
    send_cmd_no_pl(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
    send_cmd_no_pl(rpc::CommandId::CMD_MAILBOX_READ);

    rpc::payload::FileWrite fw; fw.path = "t.txt"; fw.data = etl::span<const uint8_t>(data, 4);
    send_cmd(rpc::CommandId::CMD_FILE_WRITE, fw);

    rpc::payload::FileRead fr; fr.path = "t.txt";
    send_cmd(rpc::CommandId::CMD_FILE_READ, fr);

    rpc::payload::FileRemove frm; frm.path = "t.txt";
    send_cmd(rpc::CommandId::CMD_FILE_REMOVE, frm);

    rpc::payload::ProcessRunAsync pra; pra.command = "ls";
    send_cmd(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, pra);

    send_cmd(rpc::CommandId::CMD_PROCESS_POLL, rpc::payload::ProcessPoll{1});
    send_cmd(rpc::CommandId::CMD_PROCESS_KILL, rpc::payload::ProcessKill{1});

    send_cmd_no_pl(rpc::CommandId::CMD_SPI_BEGIN);
    send_cmd_no_pl(rpc::CommandId::CMD_SPI_END);
    
    rpc::payload::SpiTransfer st; st.data = etl::span<const uint8_t>(data, 4);
    send_cmd(rpc::CommandId::CMD_SPI_TRANSFER, st);

    send_cmd(rpc::CommandId::CMD_SPI_SET_CONFIG, rpc::payload::SpiConfig{1, 2, 3});
}

void test_bridge_responses_exhaustive() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto& ba = TestAccessor::create(Bridge);
    ba.setSynchronized();
    static uint8_t buf[256];
    uint8_t ddata[] = "val";
    auto send_resp = [&](rpc::CommandId id, auto payload) {
        memset(buf, 0, sizeof(buf));
        msgpack::Encoder enc(buf, sizeof(buf));
        payload.encode(enc);
        rpc::Frame f = {};
        f.header.command_id = (uint16_t)id;
        f.payload = enc.result();
        f.header.payload_length = (uint16_t)f.payload.size();
        ba.dispatch(f);
    };
    send_resp(rpc::CommandId::CMD_FILE_READ_RESP, rpc::payload::FileReadResponse{etl::span<const uint8_t>(ddata, 3)});
    send_resp(rpc::CommandId::CMD_DATASTORE_GET_RESP, rpc::payload::DatastoreGetResponse{etl::span<const uint8_t>(ddata, 3)});
    send_resp(rpc::CommandId::CMD_MAILBOX_READ_RESP, rpc::payload::MailboxReadResponse{etl::span<const uint8_t>(ddata, 3)});
    
    rpc::payload::ProcessPollResponse ppr;
    ppr.status = static_cast<uint8_t>(rpc::StatusCode::STATUS_OK);
    ppr.exit_code = 0;
    ppr.stdout_data = etl::span<const uint8_t>(ddata, 3);
    ppr.stderr_data = etl::span<const uint8_t>(ddata, 0);
    send_resp(rpc::CommandId::CMD_PROCESS_POLL_RESP, ppr);
}

void test_bridge_observers() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    struct MockObserver : public BridgeObserver {
        void notification(MsgBridgeSynchronized) override {}
        void notification(MsgBridgeLost) override {}
    } obs;
    Bridge.registerObserver(obs);
    Bridge.notify_observers(MsgBridgeSynchronized{});
}

void test_bridge_signals() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    Bridge.signalXoff();
    Bridge.signalXon();
}

void test_service_components() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto& ba = TestAccessor::create(Bridge);
    ba.setSynchronized();
    Console.begin();
    uint8_t d[] = "hi";
    Console.write(d, 2);
    Console.process();
    DataStore.set("k", etl::span<const uint8_t>(d, 2));
    DataStore.get("k", etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<ds_handler>());
    Mailbox.push(etl::span<const uint8_t>(d, 2));
    Mailbox.requestRead();
    Mailbox.requestAvailable();
    Mailbox.signalProcessed();
    FileSystem.write("t.txt", etl::span<const uint8_t>(d, 2));
    FileSystem.read("t.txt", etl::delegate<void(etl::span<const uint8_t>)>::create<fs_handler>());
    Process.runAsync("ls", etl::span<const etl::string_view>(), etl::delegate<void(int32_t)>::create<proc_handler>());
    Process.poll(1, etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>::create<poll_handler>());
    Process.kill(1);
    SPIService.begin();
    uint8_t spidata[1] = {0};
    SPIService.transfer(etl::span<uint8_t>(spidata, 1));
    SPIService.end();
}

void test_services_direct() {
    uint8_t d[] = "data";
    rpc::payload::MailboxPush mbp; mbp.data = etl::span<const uint8_t>(d, 4);
    Mailbox._onIncomingData(mbp);
    rpc::payload::MailboxReadResponse mbr; mbr.content = etl::span<const uint8_t>(d, 4);
    Mailbox._onIncomingData(mbr);
    Mailbox._onAvailableResponse(rpc::payload::MailboxAvailableResponse{5});
    rpc::payload::FileReadResponse frr; frr.content = etl::span<const uint8_t>(d, 4);
    FileSystem._onResponse(frr);
    rpc::payload::DatastoreGetResponse dgr; dgr.value = etl::span<const uint8_t>(d, 4);
    DataStore._onResponse(dgr);
    rpc::payload::ConsoleWrite cw; cw.data = etl::span<const uint8_t>(d, 4);
    Console._push(cw);
    Process._kill(rpc::payload::ProcessKill{1});
    Process._onRunAsyncResponse(rpc::payload::ProcessRunAsyncResponse{1});
    Process._onPollResponse(rpc::payload::ProcessPollResponse{});
    Process.reset();
}

void test_bridge_errors() {
    BiStream stream;
    reset_bridge_core(Bridge, stream);
    auto& ba = TestAccessor::create(Bridge);
    ba.setSynchronized();
    rpc::Frame f = {};
    f.header.command_id = 0xFFFF;
    ba.dispatch(f);
    ba._onPacketReceived(etl::span<const uint8_t>());
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_reset_state);
  RUN_TEST(test_bridge_is_recent_duplicate_edge_cases);
  RUN_TEST(test_bridge_ack_timeout_retry);
  RUN_TEST(test_bridge_dispatch_exhaustive);
  RUN_TEST(test_bridge_responses_exhaustive);
  RUN_TEST(test_bridge_observers);
  RUN_TEST(test_bridge_signals);
  RUN_TEST(test_service_components);
  RUN_TEST(test_services_direct);
  RUN_TEST(test_bridge_errors);
  return UNITY_END();
}
