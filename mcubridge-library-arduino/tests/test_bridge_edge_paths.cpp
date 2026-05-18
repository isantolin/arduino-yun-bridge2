#define BRIDGE_ENABLE_TEST_INTERFACE
#include <etl/exception.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeFaultInjection.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "fsm/CounterIterator.h"
#include "fsm/bridge_fsm.h"
#include "protocol/rle.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"
#include "test_support.h"

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

using bridge::test::TestAccessor;

void setUp() { bridge::test::fault::reset(); }
void tearDown() {}

namespace {

struct CountingObserver final : public BridgeObserver {
  int synced = 0;
  int lost = 0;
  void notification(MsgBridgeSynchronized) override { ++synced; }
  void notification(MsgBridgeLost) override { ++lost; }
};

void on_fs_read(etl::span<const uint8_t>) {}
void on_datastore_get(etl::string_view, etl::span<const uint8_t>) {}
void on_process_poll(rpc::StatusCode, uint16_t, etl::span<const uint8_t>,
                     etl::span<const uint8_t>) {}

bool extract_encrypted_frame(const ByteBuffer<8192>& tx, size_t& cursor,
                             rpc::Frame& out, size_t attempts_left) {
  if (attempts_left == 0) return false;
  rpc::Frame candidate = {};
  if (!extract_next_valid_frame(tx, cursor, candidate)) return false;
  const uint16_t raw_cmd =
      candidate.header.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN &&
                            raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                           (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                            raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
  if (!is_excluded) {
    out = candidate;
    return true;
  }
  return extract_encrypted_frame(tx, cursor, out, attempts_left - 1);
}

rpc::Frame make_empty_frame(uint16_t cmd, uint16_t seq) {
  rpc::Frame frame = {};
  frame.header = {rpc::PROTOCOL_VERSION, 0, cmd, seq};
  frame.nonce.fill(0);
  frame.tag.fill(0);
  frame.payload = {};
  return frame;
}

template <typename T>
rpc::Frame make_payload_frame(
    uint16_t cmd, uint16_t seq, const T& payload,
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE>& storage) {
  rpc::Frame frame = {};
  frame.header = {rpc::PROTOCOL_VERSION, 0, cmd, seq};
  frame.nonce.fill(0);
  frame.tag.fill(0);
  frame.payload = etl::span<const uint8_t>(storage.data(), storage.size());
  bridge::test::set_pb_payload(frame, payload);
  return frame;
}

rpc::Frame make_malformed_payload_frame(uint16_t cmd, uint16_t seq) {
  static etl::array<uint8_t, 1> bad = {0xC1};
  rpc::Frame frame = {};
  frame.header = {rpc::PROTOCOL_VERSION, 1, cmd, seq};
  frame.nonce.fill(0);
  frame.tag.fill(0);
  frame.payload = etl::span<const uint8_t>(bad.data(), bad.size());
  return frame;
}

void test_dispatch_valid_payload_handlers_unique_seq() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  uint16_t seq = 100;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  etl::array<uint8_t, 4> bytes = {1, 2, 3, 4};

  auto set_baud =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         seq++, rpc::payload::SetBaudratePacket{57600}, buf);
  ba.dispatch(set_baud);

  auto set_pin =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),
                         seq++, rpc::payload::PinMode{255, 1}, buf);
  ba.dispatch(set_pin);

  auto dwrite =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE),
                         seq++, rpc::payload::DigitalWrite{255, 1}, buf);
  ba.dispatch(dwrite);

  auto awrite =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE),
                         seq++, rpc::payload::AnalogWrite{255, 42}, buf);
  ba.dispatch(awrite);

  auto ds = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP), seq++,
      rpc::payload::DatastoreGetResponse{
          etl::span<const uint8_t>(bytes.data(), bytes.size())},
      buf);
  ba.dispatch(ds);

  auto mpush = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH), seq++,
      rpc::payload::MailboxPush{
          etl::span<const uint8_t>(bytes.data(), bytes.size())},
      buf);
  ba.dispatch(mpush);

  auto mread = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP), seq++,
      rpc::payload::MailboxReadResponse{
          etl::span<const uint8_t>(bytes.data(), bytes.size())},
      buf);
  ba.dispatch(mread);

  auto mavl = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP), seq++,
      rpc::payload::MailboxAvailableResponse{7}, buf);
  ba.dispatch(mavl);

  auto fw = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE), seq++,
      rpc::payload::FileWrite{
          etl::span<const char>("edge.bin", 8),
          etl::span<const uint8_t>(bytes.data(), bytes.size())},
      buf);
  ba.dispatch(fw);

  auto fr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ), seq++,
      rpc::payload::FileRead{etl::span<const char>("edge.bin", 8)}, buf);
  ba.dispatch(fr);

  auto frm = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE), seq++,
      rpc::payload::FileRemove{etl::span<const char>("edge.bin", 8)}, buf);
  ba.dispatch(frm);

  auto frr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP), seq++,
      rpc::payload::FileReadResponse{
          etl::span<const uint8_t>(bytes.data(), bytes.size())},
      buf);
  ba.dispatch(frr);

  auto pr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP), seq++,
      rpc::payload::ProcessRunAsyncResponse{123}, buf);
  ba.dispatch(pr);

  auto pp = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP), seq++,
      rpc::payload::ProcessPollResponse{
          0, 0, etl::span<const uint8_t>(bytes.data(), bytes.size()),
          etl::span<const uint8_t>(bytes.data(), bytes.size())},
      buf);
  ba.dispatch(pp);

  auto pk =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL),
                         seq++, rpc::payload::ProcessKill{123}, buf);
  ba.dispatch(pk);

  auto sc =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG),
                         seq++, rpc::payload::SpiConfig{1, 0, 1000000}, buf);
  ba.dispatch(sc);

  auto sbegin = make_empty_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN), seq++);
  ba.dispatch(sbegin);

  auto send =
      make_empty_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_END), seq++);
  ba.dispatch(send);

  auto cap = make_empty_frame(
      rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES), seq++);
  ba.dispatch(cap);

  auto transfer = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER), seq++,
      rpc::payload::SpiTransfer{etl::span<const uint8_t>()}, buf);
  ba.dispatch(transfer);
}

void test_dispatch_malformed_payload_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  uint16_t seq = 300;
  const etl::array<uint16_t, 18> ids = {
      rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
      rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER),
      rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE),
      rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE),
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE),
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP),
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH),
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP),
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP),
      rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE),
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ),
      rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE),
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP),
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP),
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP),
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL),
      rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG)};

  etl::for_each(ids.begin(), ids.end(), [&ba, &seq](uint16_t cmd) {
    auto malformed = make_malformed_payload_frame(cmd, seq++);
    ba.dispatch(malformed);
  });
}

void test_packet_received_security_and_decompress_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buf;
  auto secure =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE),
                         500, rpc::payload::DigitalWrite{13, 1}, payload_buf);
  secure.crc = rpc::checksum::compute(secure);

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> wire;
  size_t wire_len = rpc::FrameParser::serialize(secure, wire);
  ba.invokePacketReceived(etl::span<const uint8_t>(wire.data(), wire_len));

  etl::array<uint8_t, 16> encoded = {0x10, 0x11, 0x12, 0x13};
  const size_t encoded_len = 4;

  rpc::Frame compressed = {};
  compressed.header = {rpc::PROTOCOL_VERSION,
                       static_cast<uint16_t>(encoded_len),
                       static_cast<uint16_t>(
                           rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION) |
                           rpc::RPC_CMD_FLAG_COMPRESSED),
                       501};
  compressed.nonce.fill(0);
  compressed.tag.fill(0);
  compressed.payload = etl::span<const uint8_t>(encoded.data(), encoded_len);
  compressed.crc = rpc::checksum::compute(compressed);

  wire_len = rpc::FrameParser::serialize(compressed, wire);
  ba.invokePacketReceived(etl::span<const uint8_t>(wire.data(), wire_len));
}

void test_console_and_policy_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  Console.begin();

  bridge::utils::CounterIterator<size_t> begin(0);
  bridge::utils::CounterIterator<size_t> end(
      bridge::config::CONSOLE_TX_BUFFER_SIZE);
  etl::for_each(begin, end, [](size_t) { (void)Console.write('x'); });

  Bridge.enterSafeState();
  TEST_ASSERT_EQUAL_UINT32(0, static_cast<uint32_t>(Console.write('z')));
  TEST_ASSERT_EQUAL(-1, Console.peek());
  TEST_ASSERT_EQUAL(-1, Console.read());

  uint8_t bytes[] = {0x41, 0x42};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  auto frame = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), 600,
      rpc::payload::ConsoleWrite{etl::span<const uint8_t>(bytes, 2)}, buf);

  ba.setSynchronized();
  ba.dispatch(frame);
  TEST_ASSERT_EQUAL(0x41, Console.peek());
  TEST_ASSERT_EQUAL(0x41, Console.read());
  TEST_ASSERT_EQUAL(0x42, Console.read());
  TEST_ASSERT_EQUAL(-1, Console.read());

  ba.applyTimingConfig(rpc::payload::HandshakeConfig{250, 2, 500});
  TEST_ASSERT_TRUE(
      ba.isSecurityCheckPassed(rpc::to_underlying(rpc::StatusCode::STATUS_OK)));

  etl::exception ex("coverage", __FILE__, __LINE__);
  bridge::SafeStatePolicy::handle(Bridge, ex);
}

void test_security_invalid_size_guards() {
  etl::array<uint8_t, 1> out = {0};
  etl::array<uint8_t, 1> in = {0x42};
  etl::array<uint8_t, 1> short_key = {0};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};

  TEST_ASSERT_FALSE(rpc::security::aead_encrypt(
      etl::span<uint8_t>(out), etl::span<uint8_t>(tag),
      etl::span<const uint8_t>(in), etl::span<const uint8_t>(short_key),
      etl::span<const uint8_t>(nonce), etl::span<const uint8_t>()));

  TEST_ASSERT_FALSE(rpc::security::aead_decrypt(
      etl::span<uint8_t>(out), etl::span<const uint8_t>(in),
      etl::span<const uint8_t>(tag), etl::span<const uint8_t>(short_key),
      etl::span<const uint8_t>(nonce), etl::span<const uint8_t>()));

  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> key = {};
  etl::array<uint8_t, 1> short_tag = {0};
  etl::array<uint8_t, 1> short_nonce = {0};
  etl::array<uint8_t, 0> tiny_out = {};
  TEST_ASSERT_FALSE(rpc::security::aead_encrypt(
      etl::span<uint8_t>(tiny_out), etl::span<uint8_t>(tag),
      etl::span<const uint8_t>(in), etl::span<const uint8_t>(key),
      etl::span<const uint8_t>(nonce), etl::span<const uint8_t>()));
  TEST_ASSERT_FALSE(rpc::security::aead_encrypt(
      etl::span<uint8_t>(out), etl::span<uint8_t>(short_tag),
      etl::span<const uint8_t>(in), etl::span<const uint8_t>(key),
      etl::span<const uint8_t>(nonce), etl::span<const uint8_t>()));
  TEST_ASSERT_FALSE(rpc::security::aead_encrypt(
      etl::span<uint8_t>(out), etl::span<uint8_t>(tag),
      etl::span<const uint8_t>(in), etl::span<const uint8_t>(key),
      etl::span<const uint8_t>(short_nonce), etl::span<const uint8_t>()));

  etl::array<uint8_t, 2> in2 = {0x01, 0x02};
  TEST_ASSERT_FALSE(rpc::security::aead_decrypt(
      etl::span<uint8_t>(out), etl::span<const uint8_t>(in2),
      etl::span<const uint8_t>(tag), etl::span<const uint8_t>(key),
      etl::span<const uint8_t>(nonce), etl::span<const uint8_t>()));
  TEST_ASSERT_FALSE(rpc::security::aead_decrypt(
      etl::span<uint8_t>(out), etl::span<const uint8_t>(in),
      etl::span<const uint8_t>(short_tag), etl::span<const uint8_t>(key),
      etl::span<const uint8_t>(nonce), etl::span<const uint8_t>()));
  TEST_ASSERT_FALSE(rpc::security::aead_decrypt(
      etl::span<uint8_t>(out), etl::span<const uint8_t>(in),
      etl::span<const uint8_t>(tag), etl::span<const uint8_t>(key),
      etl::span<const uint8_t>(short_nonce), etl::span<const uint8_t>()));
}

void test_observer_and_task_runtime_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  CountingObserver observer;
  bridge::utils::CounterIterator<size_t> it(0);
  bridge::utils::CounterIterator<size_t> end(bridge::config::MAX_OBSERVERS + 2);
  etl::for_each(it, end,
                [&observer](size_t) { Bridge.registerObserver(observer); });
  Bridge.notify_observers(MsgBridgeSynchronized());
  Bridge.notify_observers(MsgBridgeLost());
  TEST_ASSERT_EQUAL_INT(bridge::config::MAX_OBSERVERS, observer.synced);
  TEST_ASSERT_EQUAL_INT(bridge::config::MAX_OBSERVERS, observer.lost);

  ba.setSerialTaskBridgeNull();
  ba.invokeSerialTask();
  ba.setTimerTaskBridgeNull();
  ba.invokeTimerTask();

  class FlowStream : public Stream {
   public:
    int avail = 0;
    int available() override { return avail; }
    int read() override { return -1; }
    int peek() override { return -1; }
    size_t write(uint8_t) override { return 1; }
    size_t write(const uint8_t*, size_t s) override { return s; }
    void flush() override {}
  } flow;
  reset_bridge_core(Bridge, flow);
  auto ba_flow = TestAccessor::create(Bridge);
  flow.avail = bridge::config::FLOW_CONTROL_XOFF_THRESHOLD + 1;
  ba_flow.invokeSerialTask();
  flow.avail = bridge::config::FLOW_CONTROL_XON_THRESHOLD - 1;
  ba_flow.invokeSerialTask();
}

void test_timer_link_and_bootloader_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream, 0, nullptr);
  auto ba = TestAccessor::create(Bridge);
  ba.setTimerLastTick(1);
  ba.invokeTimerTask();

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  rpc::payload::LinkSync sync = {};
  sync.nonce.fill(0x11);
  sync.tag.fill(0x22);
  auto linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 700, sync, buf);
  ba.dispatch(linksync);

  rpc::payload::HandshakeConfig timing = {123, 1, 456};
  auto linkreset = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET), 701, timing, buf);
  ba.dispatch(linkreset);
  ba.applyTimingConfig(rpc::payload::HandshakeConfig{0, 0, 0});

  auto baud_zero =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         702, rpc::payload::SetBaudratePacket{0}, buf);
  ba.dispatch(baud_zero);
  auto baud_new =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         703, rpc::payload::SetBaudratePacket{115200}, buf);
  ba.dispatch(baud_new);
  auto baud_dup =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         704, rpc::payload::SetBaudratePacket{115200}, buf);
  ba.dispatch(baud_dup);

  auto boot_bad = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER), 705,
      rpc::payload::EnterBootloader{0x12345678}, buf);
  ba.dispatch(boot_bad);
  auto boot_ok = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER), 706,
      rpc::payload::EnterBootloader{rpc::RPC_BOOTLOADER_MAGIC}, buf);
  ba.dispatch(boot_ok);
}

void test_service_capacity_and_send_fail_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> frame_buf;
  auto pin_mode_ok =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),
                         780, rpc::payload::PinMode{13, 1}, frame_buf);
  ba.dispatch(pin_mode_ok);
  auto analog_write_ok =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE),
                         781, rpc::payload::AnalogWrite{13, 127}, frame_buf);
  ba.dispatch(analog_write_ok);

  Console.begin();
  Console.process();
  etl::array<uint8_t, bridge::config::CONSOLE_RX_BUFFER_SIZE + 4> console_bytes;
  console_bytes.fill(0x42);
  Console._push(rpc::payload::ConsoleWrite{
      etl::span<const uint8_t>(console_bytes.data(), console_bytes.size())});
  Console._push(rpc::payload::ConsoleWrite{
      etl::span<const uint8_t>(console_bytes.data(), console_bytes.size())});
  TEST_ASSERT_EQUAL_UINT32(0, static_cast<uint32_t>(Console.write(nullptr, 1)));
  uint8_t b = 1;
  TEST_ASSERT_EQUAL_UINT32(0, static_cast<uint32_t>(Console.write(&b, 0)));

  etl::array<uint8_t, bridge::config::MAILBOX_RX_BUFFER_SIZE + 8> mailbox_bytes;
  mailbox_bytes.fill(0x24);
  Mailbox._onIncomingData(rpc::payload::MailboxPush{
      etl::span<const uint8_t>(mailbox_bytes.data(), mailbox_bytes.size())});
  Mailbox._onIncomingData(rpc::payload::MailboxReadResponse{
      etl::span<const uint8_t>(mailbox_bytes.data(), mailbox_bytes.size())});

  Bridge.enterSafeState();
  FileSystem.read("blocked.bin",
                  FileSystemClass::FileSystemReadHandler::create<on_fs_read>());
  DataStore.get(
      "key",
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<
          on_datastore_get>());
  Process.poll(123,
               ProcessClass::ProcessPollHandler::create<on_process_poll>());
}

void test_filesystem_spi_fsm_and_rle_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 2> fs_data = {1, 2};
  FileSystem._onWrite(rpc::payload::FileWrite{
      etl::span<const char>("/bad", 4),
      etl::span<const uint8_t>(fs_data.data(), fs_data.size())});
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE + 8> big_data;
  big_data.fill(0x31);
  FileSystem._onWrite(rpc::payload::FileWrite{
      etl::span<const char>("large.bin", 9),
      etl::span<const uint8_t>(big_data.data(), big_data.size())});
  FileSystem._onRead(
      rpc::payload::FileRead{etl::span<const char>("large.bin", 9)});

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  auto spi_begin =
      make_empty_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN), 800);
  ba.dispatch(spi_begin);
  etl::array<uint8_t, 3> spi_payload = {0xA1, 0xB2, 0xC3};
  auto spi_transfer = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER), 801,
      rpc::payload::SpiTransfer{
          etl::span<const uint8_t>(spi_payload.data(), spi_payload.size())},
      buf);
  ba.dispatch(spi_transfer);
  auto spi_end =
      make_empty_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_END), 802);
  ba.dispatch(spi_end);

  bridge::fsm::BridgeFsm fsm;
  fsm.start();
  TEST_ASSERT_FALSE(fsm.isUnsynchronized());
  fsm.receive(bridge::fsm::EvReset());
  TEST_ASSERT_TRUE(fsm.isUnsynchronized());

  etl::array<uint8_t, 5> bad_rle = {rle::ESCAPE_BYTE, 10, 0x44, 0xAA, 0xBB};
  etl::array<uint8_t, 1> out = {0};
  TEST_ASSERT_EQUAL_UINT32(
      0, static_cast<uint32_t>(rle::decode(etl::span<const uint8_t>(bad_rle),
                                           etl::span<uint8_t>(out))));
}

void test_encrypted_rx_nonce_and_compressed_empty_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  rpc::payload::LinkSync sync = {};
  sync.nonce.fill(0xAB);
  ba.computeHandshakeTag(sync.nonce.data(), sync.nonce.size(), sync.tag.data());
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  auto linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 900, sync, buf);
  ba.dispatch(linksync);
  TEST_ASSERT_TRUE(ba.isSynchronized());
  ba.handleAck(ba.getLastCommandId());
  stream.tx_buf.clear();

  etl::array<uint8_t, 2> payload = {0x55, 0x66};
  TEST_ASSERT_TRUE(
      Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 901,
                  rpc::payload::ConsoleWrite{etl::span<const uint8_t>(
                      payload.data(), payload.size())}));

  size_t cursor = 0;
  rpc::Frame encrypted = {};
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> wire;
  if (extract_encrypted_frame(stream.tx_buf, cursor, encrypted, 4)) {
    size_t wire_len = rpc::FrameParser::serialize(
        encrypted, etl::span<uint8_t>(wire.data(), wire.size()));
    ba.invokePacketReceived(etl::span<const uint8_t>(wire.data(), wire_len));
  }
}

void test_fault_injection_harness_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  bridge::test::fault::set_clock_ms(0U);
  SPIService.begin();
  etl::array<uint8_t, 2> spi_buf = {0x10, 0x20};
  bridge::test::fault::enable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);
  TEST_ASSERT_EQUAL_UINT32(
      0,
      static_cast<uint32_t>(SPIService.transfer(etl::span<uint8_t>(spi_buf))));
  SPIService.end();

  etl::array<uint8_t, 80> file_data;
  file_data.fill(0x5A);
  FileSystem._onWrite(rpc::payload::FileWrite{
      etl::span<const char>("fi-timeout.bin", 14),
      etl::span<const uint8_t>(file_data.data(), file_data.size())});
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT);
  FileSystem._onRead(
      rpc::payload::FileRead{etl::span<const char>("fi-timeout.bin", 14)});

  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::KAT_SHA256_MISMATCH);
  TEST_ASSERT_FALSE(rpc::security::run_cryptographic_self_tests());
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::KAT_HMAC_MISMATCH);
  TEST_ASSERT_FALSE(rpc::security::run_cryptographic_self_tests());
  bridge::test::fault::enable(bridge::test::fault::FaultPoint::KAT_AEAD_FAIL);
  TEST_ASSERT_FALSE(rpc::security::run_cryptographic_self_tests());
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::BRIDGE_FORCE_POST_FAIL);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "top-secret");

  bridge::test::fault::reset();
  reset_bridge_core(Bridge, stream);
  auto ba2 = TestAccessor::create(Bridge);
  ba2.setSynchronized();
  ba2.exhaustTxPayloadPool();
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::BRIDGE_POOL_ALLOC_FAIL);
  TEST_ASSERT_FALSE(
      Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 77, {}));

  stream.tx_buf.clear();
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::BRIDGE_SERIALIZE_ZERO);
  Bridge.signalXoff();
  TEST_ASSERT_EQUAL_UINT32(0, static_cast<uint32_t>(stream.tx_buf.len));

  ba2.enqueueNullPendingFrame(
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), 78, 0);
  ba2.clearPendingTxQueue();

  ba2.setHardwareSerial(&Serial);
  ba2.setPendingBaudrate(115200U);
  ba2.onBaudrateChange();

  // Branch coverage for requires_ack (default path and flags)
  TEST_ASSERT_FALSE(
      rpc::requires_ack(rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION) |
                        rpc::RPC_CMD_FLAG_COMPRESSED));
  TEST_ASSERT_TRUE(
      rpc::requires_ack(rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN)));

  class FlowStream : public Stream {
   public:
    int avail = 0;
    int available() override { return avail; }
    int read() override { return -1; }
    int peek() override { return -1; }
    size_t write(uint8_t) override { return 1; }
    size_t write(const uint8_t*, size_t s) override { return s; }
    void flush() override {}
  } flow;
  reset_bridge_core(Bridge, flow);
  auto ba_flow = TestAccessor::create(Bridge);
  ba_flow.setSerialTaskXoffSent(true);
  flow.avail = bridge::config::FLOW_CONTROL_XON_THRESHOLD - 1;
  ba_flow.invokeSerialTask();

  BiStream secure_stream;
  reset_bridge_core(Bridge, secure_stream);
  auto bs = TestAccessor::create(Bridge);
  rpc::payload::LinkSync sync = {};
  sync.nonce.fill(0x44);
  bs.computeHandshakeTag(sync.nonce.data(), sync.nonce.size(), sync.tag.data());
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> tmp_buf;
  auto linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 950, sync, tmp_buf);
  bs.dispatch(linksync);
  TEST_ASSERT_TRUE(bs.isSynchronized());
  bs.handleAck(bs.getLastCommandId());
  secure_stream.tx_buf.clear();
  etl::array<uint8_t, 2> payload = {0x41, 0x42};
  TEST_ASSERT_TRUE(
      Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 951,
                  rpc::payload::ConsoleWrite{etl::span<const uint8_t>(
                      payload.data(), payload.size())}));
  size_t cursor = 0;
  rpc::Frame encrypted = {};
  if (extract_encrypted_frame(secure_stream.tx_buf, cursor, encrypted, 4)) {
    etl::array<uint8_t, rpc::MAX_FRAME_SIZE> wire;
    const size_t wire_len = rpc::FrameParser::serialize(
        encrypted, etl::span<uint8_t>(wire.data(), wire.size()));
    bridge::test::fault::enable(
        bridge::test::fault::FaultPoint::BRIDGE_NONCE_READ_FAIL);
    bs.invokePacketReceived(etl::span<const uint8_t>(wire.data(), wire_len));
  }

  rpc::Frame bad_compressed = {};
  etl::array<uint8_t, 2> bad_pl = {rle::ESCAPE_BYTE, 0x01};
  bad_compressed.header = {
      rpc::PROTOCOL_VERSION, static_cast<uint16_t>(bad_pl.size()),
      static_cast<uint16_t>(
          rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION) |
          rpc::RPC_CMD_FLAG_COMPRESSED),
      952};
  bad_compressed.payload =
      etl::span<const uint8_t>(bad_pl.data(), bad_pl.size());
  bad_compressed.nonce.fill(0);
  bad_compressed.tag.fill(0);
  bad_compressed.crc = rpc::checksum::compute(bad_compressed);
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> bad_wire;
  const size_t bad_len = rpc::FrameParser::serialize(
      bad_compressed, etl::span<uint8_t>(bad_wire.data(), bad_wire.size()));
  bs.invokePacketReceived(etl::span<const uint8_t>(bad_wire.data(), bad_len));
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_dispatch_valid_payload_handlers_unique_seq);
  RUN_TEST(test_dispatch_malformed_payload_paths);
  RUN_TEST(test_packet_received_security_and_decompress_paths);
  RUN_TEST(test_console_and_policy_edges);
  RUN_TEST(test_security_invalid_size_guards);
  RUN_TEST(test_observer_and_task_runtime_edges);
  RUN_TEST(test_timer_link_and_bootloader_edges);
  RUN_TEST(test_service_capacity_and_send_fail_edges);
  RUN_TEST(test_filesystem_spi_fsm_and_rle_edges);
  RUN_TEST(test_encrypted_rx_nonce_and_compressed_empty_paths);
  RUN_TEST(test_fault_injection_harness_paths);
  return UNITY_END();
}
