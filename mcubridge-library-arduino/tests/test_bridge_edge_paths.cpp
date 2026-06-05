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



void on_fs_read(etl::span<const uint8_t>) {}
void on_datastore_get(etl::string_view, etl::span<const uint8_t>) {}
void on_process_poll(rpc::StatusCode, uint16_t, etl::span<const uint8_t>,
                     etl::span<const uint8_t>) {}

bool extract_encrypted_frame(const ByteBuffer<8192>& tx, size_t& cursor,
                             rpc_pb_RpcEnvelope& out, size_t attempts_left) {
  if (attempts_left == 0) return false;
  rpc_pb_RpcEnvelope candidate;
  if (!extract_next_valid_frame(tx, cursor, candidate)) return false;
  const uint16_t raw_cmd =
      candidate.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
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

rpc_pb_RpcEnvelope make_empty_frame(uint16_t cmd, uint16_t seq) {
  rpc_pb_RpcEnvelope frame;
  frame.version = rpc::PROTOCOL_VERSION;
  frame.command_id = cmd;
  frame.sequence_id = seq;
  return frame;
}

template <typename T>
rpc_pb_RpcEnvelope make_payload_frame(
    uint16_t cmd, uint16_t seq, const T& payload,
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE>& storage) {
  (void)storage;
  rpc_pb_RpcEnvelope frame;
  frame.version = rpc::PROTOCOL_VERSION;
  frame.command_id = cmd;
  frame.sequence_id = seq;
  bridge::test::set_pb_payload(frame, payload);
  return frame;
}

rpc_pb_RpcEnvelope make_malformed_payload_frame(uint16_t cmd, uint16_t seq) {
  rpc_pb_RpcEnvelope frame;
  frame.version = rpc::PROTOCOL_VERSION;
  frame.command_id = cmd;
  frame.sequence_id = seq;
  frame.payload.size = 1;
  frame.payload.bytes[0] = 0xC1;
  return frame;
}

void test_dispatch_valid_payload_handlers_unique_seq() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  uint16_t seq = 100;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  

  auto set_baud =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         seq++, []() {
                           rpc::payload::SetBaudratePacket p;
                           p.baudrate = 57600;
                           return p;
                         }(),
                         buf);
  ba.dispatch(set_baud);

  auto set_pin =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),
                         seq++, []() {
                           rpc::payload::PinMode p;
                           p.pin = 255;
                           p.mode = 1;
                           return p;
                         }(),
                         buf);
  ba.dispatch(set_pin);

  auto dwrite =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE),
                         seq++, []() {
                           rpc::payload::DigitalWrite p;
                           p.pin = 255;
                           p.value = 1;
                           return p;
                         }(),
                         buf);
  ba.dispatch(dwrite);

  auto awrite =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE),
                         seq++, []() {
                           rpc::payload::AnalogWrite p;
                           p.pin = 255;
                           p.value = 42;
                           return p;
                         }(),
                         buf);
  ba.dispatch(awrite);

  auto ds = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP), seq++,
      []() {
        rpc::payload::DatastoreGetResponse p;
        uint8_t v[] = {1, 2, 3, 4};
        rpc::payload::copy_to_pb_bytes(p.value, v, 4);
        return p;
      }(),
      buf);
  ba.dispatch(ds);

  auto mpush = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH), seq++,
      []() {
        rpc::payload::MailboxPush p;
        uint8_t v[] = {1, 2, 3, 4};
        rpc::payload::copy_to_pb_bytes(p.data, v, 4);
        return p;
      }(),
      buf);
  ba.dispatch(mpush);

  auto mread = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP), seq++,
      []() {
        rpc::payload::MailboxReadResponse p;
        uint8_t v[] = {1, 2, 3, 4};
        rpc::payload::copy_to_pb_bytes(p.content, v, 4);
        return p;
      }(),
      buf);
  ba.dispatch(mread);

  auto mavl = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP), seq++,
      []() {
        rpc::payload::MailboxAvailableResponse p;
        p.count = 7;
        return p;
      }(),
      buf);
  ba.dispatch(mavl);

  auto fw = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE), seq++,
      []() {
        rpc::payload::FileWrite p;
        strncpy(p.path, "edge.bin", 64);
        uint8_t v[] = {1, 2, 3, 4};
        rpc::payload::copy_to_pb_bytes(p.data, v, 4);
        return p;
      }(),
      buf);
  ba.dispatch(fw);

  auto fr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ), seq++,
      []() {
        rpc::payload::FileRead p;
        strncpy(p.path, "edge.bin", 64);
        return p;
      }(),
      buf);
  ba.dispatch(fr);

  auto frm = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE), seq++,
      []() {
        rpc::payload::FileRemove p;
        strncpy(p.path, "edge.bin", 64);
        return p;
      }(),
      buf);
  ba.dispatch(frm);

  auto frr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP), seq++,
      []() {
        rpc::payload::FileReadResponse p;
        uint8_t v[] = {1, 2, 3, 4};
        rpc::payload::copy_to_pb_bytes(p.content, v, 4);
        return p;
      }(),
      buf);
  ba.dispatch(frr);

  auto pr = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP), seq++,
      []() {
        rpc::payload::ProcessRunAsyncResponse p;
        p.pid = 123;
        return p;
      }(),
      buf);
  ba.dispatch(pr);

  auto pp = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP), seq++,
      []() {
        rpc::payload::ProcessPollResponse p;
        p.status = 0;
        p.exit_code = 0;
        uint8_t v[] = {1, 2, 3, 4};
        rpc::payload::copy_to_pb_bytes(p.stdout_data, v, 4);
        rpc::payload::copy_to_pb_bytes(p.stderr_data, v, 4);
        return p;
      }(),
      buf);
  ba.dispatch(pp);

  auto pk = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL), seq++,
      []() {
        rpc::payload::ProcessKill p;
        p.pid = 123;
        return p;
      }(),
      buf);
  ba.dispatch(pk);

  auto sc = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG), seq++,
      []() {
        rpc::payload::SpiConfig p;
        p.frequency = 1000000;
        p.bit_order = 1;
        p.data_mode = 0;
        return p;
      }(),
      buf);
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
      rpc::payload::SpiTransfer{}, buf);
  ba.dispatch(transfer);
}

void test_dispatch_malformed_payload_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
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
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buf;
  auto secure =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE),
                         500, []() {
                           rpc::payload::DigitalWrite p;
                           p.pin = 13;
                           p.value = 1;
                           return p;
                         }(),
                         payload_buf);

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> wire;
  size_t wire_len = rpc::serialize_frame(secure, wire);
  ba.invokePacketReceived(etl::span<const uint8_t>(wire.data(), wire_len));

  etl::array<uint8_t, 4> decomp_pl = {1, 2, 3, 4};
  rpc_pb_RpcEnvelope compressed;
  compressed.version = rpc::PROTOCOL_VERSION;
  compressed.command_id = static_cast<uint16_t>(
                           rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION) |
                           rpc::RPC_CMD_FLAG_COMPRESSED);
  compressed.sequence_id = 501;
  rpc::payload::copy_to_pb_bytes(compressed.payload, decomp_pl.data(), 4);

  wire_len = rpc::serialize_frame(compressed, wire);
  ba.invokePacketReceived(etl::span<const uint8_t>(wire.data(), wire_len));
}

void test_console_and_policy_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
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
        rpc::payload::ConsoleWrite p;
        uint8_t v[] = {0x41, 0x42};
        rpc::payload::copy_to_pb_bytes(p.data, v, 2);
        return p;
      }(),
      buf);

  ba.setSynchronized();
  ba.dispatch(frame);
  TEST_ASSERT_EQUAL(0x41, Console.peek());
  TEST_ASSERT_EQUAL(0x41, Console.read());
  TEST_ASSERT_EQUAL(0x42, Console.read());
  TEST_ASSERT_EQUAL(-1, Console.read());

  ba.applyTimingConfig([]() {
    rpc::payload::HandshakeConfig p;
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
}

void test_observer_and_task_runtime_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

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
  auto& ba_flow = TestAccessor::create(Bridge);
  flow.avail = bridge::config::FLOW_CONTROL_XOFF_THRESHOLD + 1;
  ba_flow.invokeSerialTask();
  flow.avail = bridge::config::FLOW_CONTROL_XON_THRESHOLD - 1;
  ba_flow.invokeSerialTask();
}

void test_timer_link_and_bootloader_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream, 0, nullptr);
  auto& ba = TestAccessor::create(Bridge);
  ba.setTimerLastTick(1);
  ba.invokeTimerTask();

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  rpc::payload::LinkSync sync = {};
  memset(sync.nonce.bytes, 0x11, 12); sync.nonce.size = 12;
  memset(sync.tag.bytes, 0x22, 16); sync.tag.size = 16;
  auto linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 700, sync, buf);
  ba.dispatch(linksync);

  auto linkreset = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET), 701, []() {
        rpc::payload::HandshakeConfig p;
        p.ack_timeout_ms = 123;
        p.ack_retry_limit = 1;
        p.response_timeout_ms = 456;
        return p;
      }(),
      buf);
  ba.dispatch(linkreset);
  ba.applyTimingConfig([]() {
    rpc::payload::HandshakeConfig p;
    p.ack_timeout_ms = 0;
    p.ack_retry_limit = 0;
    p.response_timeout_ms = 0;
    return p;
  }());

  auto baud_new =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
                         703, []() {
                           rpc::payload::SetBaudratePacket p;
                           p.baudrate = 115200;
                           return p;
                         }(),
                         buf);
  ba.dispatch(baud_new);

  auto boot_ok = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER), 706,
      []() {
        rpc::payload::EnterBootloader p;
        p.magic = rpc::RPC_BOOTLOADER_MAGIC;
        return p;
      }(),
      buf);
  ba.dispatch(boot_ok);
}

void test_service_capacity_and_send_fail_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> frame_buf;
  auto pin_mode_ok =
      make_payload_frame(rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),
                         780, []() {
                           rpc::payload::PinMode p;
                           p.pin = 13;
                           p.mode = 1;
                           return p;
                         }(),
                         frame_buf);
  ba.dispatch(pin_mode_ok);

  Console.begin();
  Console.process();
  etl::array<uint8_t, 4> console_bytes;
  console_bytes.fill(0x42);
  rpc::payload::ConsoleWrite cmsg;
  rpc::payload::copy_to_pb_bytes(cmsg.data, console_bytes.data(), 4);
  Console._push(cmsg);

  Bridge.enterSafeState();
  FileSystem.read("blocked.bin",
                  FileSystemType::FileSystemReadHandler::create<on_fs_read>());
  DataStore.get(
      "key",
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<
          on_datastore_get>());
  Process.poll(123,
               ProcessType::ProcessPollHandler::create<on_process_poll>());
}

void test_filesystem_spi_fsm_and_rle_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 2> fs_data = {1, 2};
  rpc::payload::FileWrite fwp;
  strncpy(fwp.path, "/bad", 64);
  rpc::payload::copy_to_pb_bytes(fwp.data, fs_data.data(),
                                 fs_data.size());
  FileSystem._onWrite(fwp);

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  auto spi_begin =
      make_empty_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN), 800);
  ba.dispatch(spi_begin);
  etl::array<uint8_t, 3> spi_payload = {0xA1, 0xB2, 0xC3};
  rpc::payload::SpiTransfer stp;
  rpc::payload::copy_to_pb_bytes(stp.data, spi_payload.data(),
                                 spi_payload.size());
  auto spi_transfer = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER), 801, stp, buf);
  ba.dispatch(spi_transfer);
  auto spi_end =
      make_empty_frame(rpc::to_underlying(rpc::CommandId::CMD_SPI_END), 802);
  ba.dispatch(spi_end);

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
  auto& ba = TestAccessor::create(Bridge);

  rpc::payload::LinkSync sync = {};
  memset(sync.nonce.bytes, 0xAB, 12); sync.nonce.size = 12;
  ba.computeHandshakeTag(sync.nonce.bytes, 12, sync.tag.bytes);
  sync.tag.size = 16;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
  auto linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 900, sync, buf);
  ba.dispatch(linksync);
  TEST_ASSERT_TRUE(ba.isSynchronized());
  ba.handleAck(ba.getLastCommandId());
  stream.tx_buf.clear();

  etl::array<uint8_t, 2> payload = {0x55, 0x66};
  rpc::payload::ConsoleWrite cmsg;
  rpc::payload::copy_to_pb_bytes(cmsg.data, payload.data(),
                                 payload.size());
  TEST_ASSERT_TRUE(
      Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 901, cmsg));

  size_t cursor = 0;
  rpc_pb_RpcEnvelope encrypted;
  if (extract_encrypted_frame(stream.tx_buf, cursor, encrypted, 4)) {
    etl::array<uint8_t, rpc::MAX_FRAME_SIZE> wire;
    size_t wire_len = rpc::serialize_frame(
        encrypted, etl::span<uint8_t>(wire.data(), wire.size()));
    ba.invokePacketReceived(etl::span<const uint8_t>(wire.data(), wire_len));
  }
}

void test_fault_injection_harness_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  bridge::test::fault::set_clock_ms(0U);
  SPIService.begin();
  etl::array<uint8_t, 2> spi_buf = {0x10, 0x20};
  bridge::test::fault::enable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);
  TEST_ASSERT_EQUAL_UINT32(
      0,
      static_cast<uint32_t>(SPIService.transfer(etl::span<uint8_t>(spi_buf))));
  SPIService.end();

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
  auto& ba2 = TestAccessor::create(Bridge);
  ba2.setSynchronized();
  ba2.exhaustTxPayloadPool();
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::BRIDGE_POOL_ALLOC_FAIL);
  TEST_ASSERT_FALSE(
      Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 77, {}));

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
  auto& ba_flow = TestAccessor::create(Bridge);
  ba_flow.setSerialTaskXoffSent(true);
  flow.avail = bridge::config::FLOW_CONTROL_XON_THRESHOLD - 1;
  ba_flow.invokeSerialTask();

  BiStream secure_stream;
  reset_bridge_core(Bridge, secure_stream);
  auto& bs = TestAccessor::create(Bridge);
  rpc::payload::LinkSync s_sync = {};
  memset(s_sync.nonce.bytes, 0x44, 12); s_sync.nonce.size = 12;
  bs.computeHandshakeTag(s_sync.nonce.bytes, 12, s_sync.tag.bytes);
  s_sync.tag.size = 16;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> tmp_buf;
  auto s_linksync = make_payload_frame(
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 950, s_sync, tmp_buf);
  bs.dispatch(s_linksync);
  TEST_ASSERT_TRUE(bs.isSynchronized());
  bs.handleAck(bs.getLastCommandId());
  secure_stream.tx_buf.clear();
  etl::array<uint8_t, 2> s_payload = {0x41, 0x42};
  rpc::payload::ConsoleWrite s_cmsg;
  rpc::payload::copy_to_pb_bytes(s_cmsg.data, s_payload.data(),
                                 s_payload.size());
  TEST_ASSERT_TRUE(
      Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 951, s_cmsg));
  size_t cursor = 0;
  rpc_pb_RpcEnvelope s_encrypted;
  if (extract_encrypted_frame(secure_stream.tx_buf, cursor, s_encrypted, 4)) {
    etl::array<uint8_t, rpc::MAX_FRAME_SIZE> wire;
    const size_t wire_len = rpc::serialize_frame(
        s_encrypted, etl::span<uint8_t>(wire.data(), wire.size()));
    bridge::test::fault::enable(
        bridge::test::fault::FaultPoint::BRIDGE_NONCE_READ_FAIL);
    bs.invokePacketReceived(etl::span<const uint8_t>(wire.data(), wire_len));
  }

  rpc_pb_RpcEnvelope bad_compressed;
  bad_compressed.version = rpc::PROTOCOL_VERSION;
  bad_compressed.command_id = static_cast<uint16_t>(
          rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION) |
          rpc::RPC_CMD_FLAG_COMPRESSED);
  bad_compressed.sequence_id = 952;
  uint8_t bad_pl[] = {rle::ESCAPE_BYTE, 0x01};
  rpc::payload::copy_to_pb_bytes(bad_compressed.payload, bad_pl, 2);
  
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> bad_wire;
  const size_t bad_len = rpc::serialize_frame(
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
