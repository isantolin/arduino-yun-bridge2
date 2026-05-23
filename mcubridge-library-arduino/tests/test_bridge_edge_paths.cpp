#define BRIDGE_ENABLE_TEST_INTERFACE
#include <etl/exception.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeFaultInjection.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "etl_ext/CounterIterator.h"
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
  static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> frame_buf;
  frame.payload = etl::span<uint8_t>(frame_buf.data(), frame_buf.size());
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
  

  auto set_baud =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         seq++, []() {
                           rpc_pb_SetBaudratePacket p;
                           p.baudrate = 57600;
                           return p;
                         }(),
                         buf);
  ba.invokePacketReceived(set_baud.payload);

  auto set_pin =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),
                         seq++, []() {
                           rpc_pb_PinMode p;
                           p.pin = 255;
                           p.mode = 1;
                           return p;
                         }(),
                         buf);
  ba.invokePacketReceived(set_pin.payload);

  auto dwrite =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE),
                         seq++, []() {
                           rpc_pb_DigitalWrite p;
                           p.pin = 255;
                           p.value = 1;
                           return p;
                         }(),
                         buf);
  ba.invokePacketReceived(dwrite.payload);

  auto awrite =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE),
                         seq++, []() {
                           rpc_pb_AnalogWrite p;
                           p.pin = 255;
                           p.value = 42;
                           return p;
                         }(),
                         buf);
  ba.invokePacketReceived(awrite.payload);

  auto ds = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP), seq++,
      []() {
        rpc_pb_DatastoreGetResponse p;
        uint8_t v[] = {1, 2, 3, 4};
        copy_to_pb_bytes(p.value, v, 4);
        return p;
      }(),
      buf);
  ba.invokePacketReceived(ds.payload);

  auto mpush = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH), seq++,
      []() {
        rpc_pb_MailboxPush p;
        uint8_t v[] = {1, 2, 3, 4};
        copy_to_pb_bytes(p.data, v, 4);
        return p;
      }(),
      buf);
  ba.invokePacketReceived(mpush.payload);

  auto mread = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP), seq++,
      []() {
        rpc_pb_MailboxReadResponse p;
        uint8_t v[] = {1, 2, 3, 4};
        copy_to_pb_bytes(p.content, v, 4);
        return p;
      }(),
      buf);
  ba.invokePacketReceived(mread.payload);

  auto mavl = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP), seq++,
      []() {
        rpc_pb_MailboxAvailableResponse p;
        p.count = 7;
        return p;
      }(),
      buf);
  ba.invokePacketReceived(mavl.payload);

  auto fw = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE), seq++,
      []() {
        rpc_pb_FileWrite p;
        strncpy(p.path, "edge.bin", 64);
        uint8_t v[] = {1, 2, 3, 4};
        copy_to_pb_bytes(p.data, v, 4);
        return p;
      }(),
      buf);
  ba.invokePacketReceived(fw.payload);

  auto fr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ), seq++,
      []() {
        rpc_pb_FileRead p;
        strncpy(p.path, "edge.bin", 64);
        return p;
      }(),
      buf);
  ba.invokePacketReceived(fr.payload);

  auto frm = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE), seq++,
      []() {
        rpc_pb_FileRemove p;
        strncpy(p.path, "edge.bin", 64);
        return p;
      }(),
      buf);
  ba.invokePacketReceived(frm.payload);

  auto frr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP), seq++,
      []() {
        rpc_pb_FileReadResponse p;
        uint8_t v[] = {1, 2, 3, 4};
        copy_to_pb_bytes(p.content, v, 4);
        return p;
      }(),
      buf);
  ba.invokePacketReceived(frr.payload);

  auto pr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP), seq++,
      []() {
        rpc_pb_ProcessRunAsyncResponse p;
        p.pid = 123;
        return p;
      }(),
      buf);
  ba.invokePacketReceived(pr.payload);

  auto pp = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP), seq++,
      []() {
        rpc_pb_ProcessPollResponse p;
        p.status = 0;
        p.exit_code = 0;
        uint8_t v[] = {1, 2, 3, 4};
        copy_to_pb_bytes(p.stdout_data, v, 4);
        copy_to_pb_bytes(p.stderr_data, v, 4);
        return p;
      }(),
      buf);
  ba.invokePacketReceived(pp.payload);

  auto pk = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL), seq++,
      []() {
        rpc_pb_ProcessKill p;
        p.pid = 123;
        return p;
      }(),
      buf);
  ba.invokePacketReceived(pk.payload);

  auto sc = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG), seq++,
      []() {
        rpc_pb_SpiConfig p;
        p.frequency = 1000000;
        p.bit_order = 1;
        p.data_mode = 0;
        return p;
      }(),
      buf);
  ba.invokePacketReceived(sc.payload);

  auto sbegin = make_empty_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN), seq++);
  ba.invokePacketReceived(sbegin.payload);

  auto send =
      make_empty_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_END), seq++);
  ba.invokePacketReceived(send.payload);

  auto cap = make_empty_frame(
      rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES), seq++);
  ba.invokePacketReceived(cap.payload);

  auto transfer = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER), seq++,
      rpc_pb_SpiTransfer{}, buf);
  ba.invokePacketReceived(transfer.payload);
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
    ba.invokePacketReceived(malformed.payload);
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
                         500, []() {
                           rpc_pb_DigitalWrite p;
                           p.pin = 13;
                           p.value = 1;
                           return p;
                         }(),
                         payload_buf);
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

  bridge::etl_ext::CounterIterator<size_t> begin(0);
  bridge::etl_ext::CounterIterator<size_t> end(
      bridge::config::CONSOLE_TX_BUFFER_SIZE);
  etl::for_each(begin, end, [](size_t) { (void)Console.write('x'); });

  Bridge.enterSafeState();
  TEST_ASSERT_EQUAL_UINT32(0, static_cast<uint32_t>(Console.write('z')));
  TEST_ASSERT_EQUAL(-1, Console.peek());
  TEST_ASSERT_EQUAL(-1, Console.read());

  
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  auto frame = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), 600,
      []() {
        rpc_pb_ConsoleWrite p;
        uint8_t v[] = {0x41, 0x42};
        copy_to_pb_bytes(p.data, v, 2);
        return p;
      }(),
      buf);

  ba.setSynchronized();
  ba.invokePacketReceived(frame.payload);
  TEST_ASSERT_EQUAL(0x41, Console.peek());
  TEST_ASSERT_EQUAL(0x41, Console.read());
  TEST_ASSERT_EQUAL(0x42, Console.read());
  TEST_ASSERT_EQUAL(-1, Console.read());

  ba.applyTimingConfig([]() {
    rpc_pb_HandshakeConfig p;
    p.ack_timeout_ms = 250;
    p.ack_retry_limit = 2;
    p.response_timeout_ms = 500;
    return p;
  }());
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
  LinkSync sync = {};
  memset(sync.nonce.bytes, 0x11, 16); sync.nonce.size = 16;
  memset(sync.tag.bytes, 0x22, 16); sync.tag.size = 16;
  auto linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 700, sync, buf);
  ba.invokePacketReceived(linksync.payload);

  auto linkreset = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET), 701, []() {
        rpc_pb_HandshakeConfig p;
        p.ack_timeout_ms = 123;
        p.ack_retry_limit = 1;
        p.response_timeout_ms = 456;
        return p;
      }(),
      buf);
  ba.invokePacketReceived(linkreset.payload);
  ba.applyTimingConfig([]() {
    rpc_pb_HandshakeConfig p;
    p.ack_timeout_ms = 0;
    p.ack_retry_limit = 0;
    p.response_timeout_ms = 0;
    return p;
  }());

  auto baud_zero =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         702, []() {
                           rpc_pb_SetBaudratePacket p;
                           p.baudrate = 0;
                           return p;
                         }(),
                         buf);
  ba.invokePacketReceived(baud_zero.payload);
  auto baud_new =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         703, []() {
                           rpc_pb_SetBaudratePacket p;
                           p.baudrate = 115200;
                           return p;
                         }(),
                         buf);
  ba.invokePacketReceived(baud_new.payload);
  auto baud_dup =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         704, []() {
                           rpc_pb_SetBaudratePacket p;
                           p.baudrate = 115200;
                           return p;
                         }(),
                         buf);
  ba.invokePacketReceived(baud_dup.payload);

  auto boot_bad = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER), 705,
      []() {
        rpc_pb_EnterBootloader p;
        p.magic = 0x12345678;
        return p;
      }(),
      buf);
  ba.invokePacketReceived(boot_bad.payload);
  auto boot_ok = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER), 706,
      []() {
        rpc_pb_EnterBootloader p;
        p.magic = rpc::RPC_BOOTLOADER_MAGIC;
        return p;
      }(),
      buf);
  ba.invokePacketReceived(boot_ok.payload);
}

void test_service_capacity_and_send_fail_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> frame_buf;
  auto pin_mode_ok =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),
                         780, []() {
                           rpc_pb_PinMode p;
                           p.pin = 13;
                           p.mode = 1;
                           return p;
                         }(),
                         frame_buf);
  ba.invokePacketReceived(pin_mode_ok.payload);
  auto analog_write_ok =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE),
                         781, []() {
                           rpc_pb_AnalogWrite p;
                           p.pin = 13;
                           p.value = 127;
                           return p;
                         }(),
                         frame_buf);
  ba.invokePacketReceived(analog_write_ok.payload);

  Console.begin();
  Console.process();
  etl::array<uint8_t, bridge::config::CONSOLE_RX_BUFFER_SIZE + 4> console_bytes;
  console_bytes.fill(0x42);
  rpc_pb_ConsoleWrite cmsg;
  copy_to_pb_bytes(cmsg.data, console_bytes.data(),
                                 console_bytes.size());
  Console._push(cmsg);
  Console._push(cmsg);
  TEST_ASSERT_EQUAL_UINT32(0, static_cast<uint32_t>(Console.write(nullptr, 1)));
  uint8_t b = 1;
  TEST_ASSERT_EQUAL_UINT32(0, static_cast<uint32_t>(Console.write(&b, 0)));

  etl::array<uint8_t, bridge::config::MAILBOX_RX_BUFFER_SIZE + 8> mailbox_bytes;
  mailbox_bytes.fill(0x24);
  rpc_pb_MailboxPush mpush;
  copy_to_pb_bytes(mpush.data, mailbox_bytes.data(),
                                 mailbox_bytes.size());
  Mailbox._onIncomingData(mpush);
  rpc_pb_MailboxReadResponse mread;
  copy_to_pb_bytes(mread.content, mailbox_bytes.data(),
                                 mailbox_bytes.size());
  Mailbox._onIncomingData(mread);

  Bridge.enterSafeState();
  FileSystem.read("blocked.bin",
                  FileSystemClass::FileSystemReadHandler::create<on_fs_read>());
  DataStore.get(
      "key",
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<
          on_datastore_get>());
  Process.poll(123,
               ProcessClass::rpc_pb_ProcessPollHandler::create<on_process_poll>());
}

void test_filesystem_spi_fsm_and_rle_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 2> fs_data = {1, 2};
  rpc_pb_FileWrite fwp;
  strncpy(fwp.path, "/bad", 64);
  copy_to_pb_bytes(fwp.data, fs_data.data(),
                                 fs_data.size());
  FileSystem._onWrite(fwp);

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE + 8> big_data;
  big_data.fill(0x31);
  rpc_pb_FileWrite fwp2;
  strncpy(fwp2.path, "large.bin", 64);
  copy_to_pb_bytes(fwp2.data, big_data.data(),
                                 big_data.size());
  FileSystem._onWrite(fwp2);

  rpc_pb_FileRead frp;
  strncpy(frp.path, "large.bin", 64);
  FileSystem._onRead(frp);

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  auto spi_begin =
      make_empty_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN), 800);
  ba.invokePacketReceived(spi_begin.payload);
  etl::array<uint8_t, 3> spi_payload = {0xA1, 0xB2, 0xC3};
  rpc_pb_SpiTransfer stp;
  copy_to_pb_bytes(stp.data, spi_payload.data(),
                                 spi_payload.size());
  auto spi_transfer = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER), 801, stp, buf);
  ba.invokePacketReceived(spi_transfer.payload);
  auto spi_end =
      make_empty_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_END), 802);
  ba.invokePacketReceived(spi_end.payload);

  bridge::fsm::BridgeFsm fsm;
  fsm.start();
  fsm.receive(bridge::fsm::EvReset());

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

  LinkSync sync = {};
  memset(sync.nonce.bytes, 0xAB, 16); sync.nonce.size = 16;
  sync.nonce.size = 16;
  ba.computeHandshakeTag(sync.nonce.bytes, 16, sync.tag.bytes);
  sync.tag.size = 16;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  auto linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 900, sync, buf);
  ba.invokePacketReceived(linksync.payload);
  TEST_ASSERT_TRUE(ba.isSynchronized());
  ba.handleAck(ba.getLastCommandId());
  stream.tx_buf.clear();

  etl::array<uint8_t, 2> payload = {0x55, 0x66};
  rpc_pb_ConsoleWrite cmsg;
  copy_to_pb_bytes(cmsg.data, payload.data(),
                                 payload.size());
  TEST_ASSERT_TRUE(
      Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 901, cmsg));

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
  rpc_pb_FileWrite fwp;
  strncpy(fwp.path, "fi-timeout.bin", 64);
  copy_to_pb_bytes(fwp.data, file_data.data(),
                                 file_data.size());
  FileSystem._onWrite(fwp);
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT);
  rpc_pb_FileRead frp;
  strncpy(frp.path, "fi-timeout.bin", 64);
  FileSystem._onRead(frp);

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
  LinkSync s_sync = {};
  memset(s_sync.nonce.bytes, 0x44, 16); s_sync.nonce.size = 16;
  s_sync.nonce.size = 16;
  bs.computeHandshakeTag(s_sync.nonce.bytes, 16, s_sync.tag.bytes);
  s_sync.tag.size = 16;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> tmp_buf;
  auto s_linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 950, s_sync, tmp_buf);
  bs.dispatch(s_linksync);
  TEST_ASSERT_TRUE(bs.isSynchronized());
  bs.handleAck(bs.getLastCommandId());
  secure_stream.tx_buf.clear();
  etl::array<uint8_t, 2> s_payload = {0x41, 0x42};
  rpc_pb_ConsoleWrite s_cmsg;
  copy_to_pb_bytes(s_cmsg.data, s_payload.data(),
                                 s_payload.size());
  TEST_ASSERT_TRUE(
      Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 951, s_cmsg));
  size_t cursor = 0;
  rpc::Frame s_encrypted = {};
  if (extract_encrypted_frame(secure_stream.tx_buf, cursor, s_encrypted, 4)) {
    etl::array<uint8_t, rpc::MAX_FRAME_SIZE> wire;
    const size_t wire_len = rpc::FrameParser::serialize(
        s_encrypted, etl::span<uint8_t>(wire.data(), wire.size()));
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
  bad_compressed.payload = etl::span<const uint8_t>(bad_pl.data(), bad_pl.size());
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