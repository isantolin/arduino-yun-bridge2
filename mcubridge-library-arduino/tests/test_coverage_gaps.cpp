/*
 * test_coverage_gaps.cpp - Comprehensive coverage gap filler
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

static unsigned long g_millis = 10000;
unsigned long millis() { return g_millis; }

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "protocol/BridgeEvents.h"
#include "protocol/rle.h"
#include "protocol/rpc_cobs.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "router/command_router.h"
#include "security/security.h"
#include "test_support.h"

// --- GLOBALS ---
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

class TestStream : public Stream {
 public:
  ByteBuffer<8192> tx;
  ByteBuffer<8192> rx;
  size_t write(uint8_t c) override {
    tx.push(c);
    return 1;
  }
  size_t write(const uint8_t* b, size_t s) override {
    tx.append(b, s);
    return s;
  }
  int available() override { return rx.remaining(); }
  int read() override { return rx.read_byte(); }
  int peek() override { return rx.peek_byte(); }
  void flush() override {}
  void feed(const uint8_t* b, size_t s) { rx.append(b, s); }
};

void reset_env(TestStream& stream) {
  g_arduino_stream_delegate = &stream;
  Bridge.begin();
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setIdle();
  Console.begin();
#if BRIDGE_ENABLE_DATASTORE
  DataStore.reset();
#endif
#if BRIDGE_ENABLE_PROCESS
  Process.reset();
#endif
}

void test_gpio_commands_via_dispatch() {
  printf("  -> test_gpio_commands_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  f.header.payload_length = rpc::payload::PinMode::SIZE;
  f.payload[0] = 13;
  f.payload[1] = 1;
  ba.dispatch(f);

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.header.payload_length = rpc::payload::DigitalWrite::SIZE;
  f.payload[0] = 13;
  f.payload[1] = 1;
  ba.dispatch(f);

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE);
  f.header.payload_length = rpc::payload::AnalogWrite::SIZE;
  f.payload[0] = 9;
  rpc::write_u16_be(&f.payload[1], 128);
  ba.dispatch(f);

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ);
  f.header.payload_length = rpc::payload::PinRead::SIZE;
  f.payload[0] = 7;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ);
  f.header.payload_length = rpc::payload::PinRead::SIZE;
  f.payload[0] = 0;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);
}

void test_console_write_via_dispatch() {
  printf("  -> test_console_write_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  const char* msg = "Hello Console";
  f.header.payload_length = static_cast<uint16_t>(strlen(msg));
  memcpy(f.payload.data(), msg, f.header.payload_length);
  ba.dispatch(f);
}

void test_datastore_resp_via_dispatch() {
  printf("  -> test_datastore_resp_via_dispatch\n");
#if BRIDGE_ENABLE_DATASTORE
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  DataStore.onDataStoreGetResponse(
      DataStoreClass::DataStoreGetHandler::create([](etl::string_view,
                                                     etl::span<const uint8_t>) {
      }));
  DataStore.requestGet("mykey");

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
  f.header.payload_length = 4;
  f.payload[0] = 3;
  f.payload[1] = 'a';
  f.payload[2] = 'b';
  f.payload[3] = 'c';
  ba.dispatch(f);
#endif
}

void test_mailbox_via_dispatch() {
  printf("  -> test_mailbox_via_dispatch\n");
#if BRIDGE_ENABLE_MAILBOX
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  Mailbox.onMailboxMessage(
      MailboxClass::MailboxHandler::create([](etl::span<const uint8_t>) {}));

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH);
  f.header.payload_length = 5;
  rpc::write_u16_be(f.payload.data(), 3);
  f.payload[2] = 'x';
  f.payload[3] = 'y';
  f.payload[4] = 'z';
  ba.dispatch(f);

  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  f.header.payload_length = 4;
  rpc::write_u16_be(f.payload.data(), 2);
  f.payload[2] = 'A';
  f.payload[3] = 'B';
  ba.dispatch(f);

  Mailbox.onMailboxAvailableResponse(
      MailboxClass::MailboxAvailableHandler::create([](uint16_t) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload.data(), 42);
  ba.dispatch(f);
#endif
}

void test_filesystem_via_dispatch() {
  printf("  -> test_filesystem_via_dispatch\n");
#if BRIDGE_ENABLE_FILESYSTEM
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE);
  f.header.payload_length = 6;
  f.payload[0] = 1;  // path len
  f.payload[1] = 'a';
  rpc::write_u16_be(&f.payload[2], 2);
  f.payload[4] = 'd';
  f.payload[5] = 'e';
  ba.dispatch(f);

  FileSystem.onFileSystemReadResponse(
      FileSystemClass::FileSystemReadHandler::create(
          [](etl::span<const uint8_t>) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
  f.header.payload_length = 4;
  rpc::write_u16_be(f.payload.data(), 2);
  f.payload[2] = 'O';
  f.payload[3] = 'K';
  ba.dispatch(f);
#endif
}

void test_process_via_dispatch() {
  printf("  -> test_process_via_dispatch\n");
#if BRIDGE_ENABLE_PROCESS
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  Process.onProcessRunAsyncResponse(
      ProcessClass::ProcessRunAsyncHandler::create([](int16_t) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload.data(), 99);
  ba.dispatch(f);

  Process.onProcessPollResponse(ProcessClass::ProcessPollHandler::create(
      [](rpc::StatusCode, uint8_t, etl::span<const uint8_t>,
         etl::span<const uint8_t>) {}));
  auto pa = bridge::test::ProcessTestAccessor::create(Process);
  pa.pushPendingPid(42);
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
  f.header.payload_length = 8;
  f.payload[0] = 0x30;
  f.payload[1] = 0;
  rpc::write_u16_be(&f.payload[2], 1);
  f.payload[4] = 'o';
  rpc::write_u16_be(&f.payload[5], 1);
  f.payload[7] = 'e';
  ba.dispatch(f);
#endif
}

void test_unknown_command_via_dispatch() {
  printf("  -> test_unknown_command_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = 0xFF;
  f.header.payload_length = 0;

  ba.dispatch(f);
}

void test_system_commands_via_dispatch() {
  printf("  -> test_system_commands_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
  f.header.payload_length = 0;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY);
  f.header.payload_length = 0;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
  f.header.payload_length = 0;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE);
  f.header.payload_length = 4;
  rpc::write_u32_be(f.payload.data(), 57600);
  ba.dispatch(f);
}

void test_ack_and_retransmit() {
  printf("  -> test_ack_and_retransmit\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  uint8_t payload[] = {13, 1};
  Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE, etl::span<const uint8_t>(payload, 2));
  TEST_ASSERT(ba.isAwaitingAck());

  uint16_t last_cmd = ba.getLastCommandId();
  rpc::Frame ack_frame;
  memset(&ack_frame, 0, sizeof(ack_frame));
  ack_frame.header.command_id =
      rpc::to_underlying(rpc::StatusCode::STATUS_ACK);
  ack_frame.header.payload_length = 2;
  rpc::write_u16_be(ack_frame.payload.data(), last_cmd);
  ba.dispatch(ack_frame);
  TEST_ASSERT(ba.isIdle());

  Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE, etl::span<const uint8_t>(payload, 2));
  TEST_ASSERT(ba.isAwaitingAck());
  ba.setAckRetryLimit(2);
  ba.setRetryCount(0);
  ba.onAckTimeout();
  TEST_ASSERT(ba.isAwaitingAck());

  ba.setRetryCount(ba.getAckRetryLimit());
  ba.onAckTimeout();
  TEST_ASSERT(ba.isUnsynchronized());
}

void test_ack_timeout_with_status_handler() {
  printf("  -> test_ack_timeout_with_status_handler\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  uint8_t payload[] = {13, 1};
  Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE, etl::span<const uint8_t>(payload, 2));
  ba.setAckRetryLimit(1);
  ba.setRetryCount(1);
  ba.onAckTimeout();
}

void test_timer_callbacks() {
  printf("  -> test_timer_callbacks\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.crc = 0xDEADBEEF;
  ba.markRxProcessed(f);
  TEST_ASSERT(ba.getRxHistorySize() > 0);
  ba.onRxDedupe();
  TEST_ASSERT_EQ_UINT(ba.getRxHistorySize(), 0);

  ba.setPendingBaudrate(9600);
  ba.onBaudrateChange();
  TEST_ASSERT_EQ_UINT(ba.getPendingBaudrate(), 0);
}

void test_fsm_transitions() {
  printf("  -> test_fsm_transitions\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  ba.setIdle();
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  ba.fsmResetFsm();
  TEST_ASSERT(ba.isUnsynchronized());

  ba.fsmHandshakeStart();
  ba.fsmHandshakeFailed();
  TEST_ASSERT(ba.isFault());

  ba.fsmResetFsm();
  ba.fsmHandshakeStart();
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  ba.fsmResetFsm();
  ba.setIdle();
  ba.fsmSendCritical();
  TEST_ASSERT(ba.isAwaitingAck());
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  ba.fsmResetFsm();
  TEST_ASSERT(ba.isUnsynchronized());
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  ba.fsmResetFsm();
  ba.setIdle();
  ba.fsmSendCritical();
  TEST_ASSERT(ba.isAwaitingAck());
  ba.setLastCommandId(rpc::RPC_INVALID_ID_SENTINEL); ba.handleAck(rpc::RPC_INVALID_ID_SENTINEL);
  TEST_ASSERT(ba.isIdle());
}

struct TestObserver : public BridgeObserver {
  bool lost_called = false;
  bool sync_called = false;
  void notification(MsgBridgeSynchronized) override { sync_called = true; }
  void notification(MsgBridgeLost) override { lost_called = true; }
  void notification(MsgBridgeError) override {}
};

void test_observer_notifications() {
  printf("  -> test_observer_notifications\n");
  TestStream stream;
  reset_env(stream);

  TestObserver obs;
  Bridge.add_observer(obs);
  Bridge.enterSafeState();
  TEST_ASSERT(obs.lost_called);
  Bridge.remove_observer(obs);
}

void test_bridge_events_defaults() {
  printf("  -> test_bridge_events_defaults\n");
  struct MinimalObserver : public BridgeObserver {
    void notification(MsgBridgeError) override {}
  };
  {
    MinimalObserver obs;
    BridgeObserver& base = obs;
    base.notification(MsgBridgeSynchronized{});
    base.notification(MsgBridgeLost{});
  }
}

void test_hal_free_memory() {
  printf("  -> test_hal_free_memory\n");
  uint16_t mem = bridge::hal::getFreeMemory();
  TEST_ASSERT_EQ_UINT(mem, 4096);
  TEST_ASSERT_EQ_UINT(getFreeMemory(), 4096);
}

void test_apply_timing_config() {
  printf("  -> test_apply_timing_config\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  ba.applyTimingConfig(nullptr, 0);
  TEST_ASSERT(ba.getAckTimeoutMs() > 0);

  uint8_t config[7];
  rpc::write_u16_be(config, 500);
  config[2] = 3;
  rpc::write_u32_be(config + 3, 5000);
  ba.applyTimingConfig(config, 7);
  TEST_ASSERT_EQ_UINT(ba.getAckTimeoutMs(), 500);

  rpc::write_u16_be(config, 1);
  config[2] = 0;
  rpc::write_u32_be(config + 3, 1);
  ba.applyTimingConfig(config, 7);
}

void test_send_key_val_command() {
  printf("  -> test_send_key_val_command\n");
#if BRIDGE_ENABLE_DATASTORE
  TestStream stream;
  reset_env(stream);
  DataStore.put("testkey", "testval");
#endif
}

void test_emit_status() {
  printf("  -> test_emit_status\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE) |
                        rpc::RPC_CMD_FLAG_COMPRESSED;
  f.header.payload_length = 1;
  f.payload[0] = 0xFF;
  ba.dispatch(f);

  ba.setIdle();
  ba.assignSharedSecret((const uint8_t*)"mysecret",
                        (const uint8_t*)"mysecret" + 8);
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  f.header.payload_length = 2;
  ba.dispatch(f);

  reset_env(stream);
  {
  auto ba2 = bridge::test::TestAccessor::create(Bridge);
  const uint8_t secret[] = "test_secret_key!";
  ba2.assignSharedSecret(secret, secret + 16);

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  f.header.payload_length = rpc::RPC_HANDSHAKE_NONCE_LENGTH +
                             rpc::RPC_HANDSHAKE_TAG_LENGTH;
  memset(f.payload.data(), 0xAA, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  memset(f.payload.data() + rpc::RPC_HANDSHAKE_NONCE_LENGTH, 0xBB,
         rpc::RPC_HANDSHAKE_TAG_LENGTH);
  ba2.dispatch(f);
  }
}

void test_emit_status_with_observer() {
  printf("  -> test_emit_status_with_observer\n");
  TestStream stream;
  reset_env(stream);

  struct ErrorObserver : public BridgeObserver {
    bool error_called = false;
    void notification(MsgBridgeError) override { error_called = true; }
  };

  ErrorObserver obs;
  Bridge.add_observer(obs);

  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, etl::string_view("test"));
  TEST_ASSERT(obs.error_called);

  Bridge.remove_observer(obs);
}

void test_cobs_overflow_in_process() {
  printf("  -> test_cobs_overflow_in_process\n");
  TestStream stream;
  reset_env(stream);

  uint8_t delim = 0x00;
  stream.feed(&delim, 1);

  for (int block = 0; block < 10; ++block) {
    uint8_t code = 0xFF;
    stream.feed(&code, 1);
    uint8_t data[254];
    memset(data, 0x42, 254);
    stream.feed(data, 254);
  }

  stream.feed(&delim, 1);

  g_millis += 10;
  Bridge.process();
}

void test_cobs_decode() {
  printf("  -> test_cobs_decode\n");

  uint8_t src[] = {1, 2, 0, 3, 4};
  uint8_t encoded[20];
  uint8_t decoded[20];

  size_t enc_len = rpc::cobs::encode(etl::span<const uint8_t>(src, 5),
                                     etl::span<uint8_t>(encoded, 20));
  TEST_ASSERT(enc_len > 0);

  size_t dec_len = rpc::cobs::decode(etl::span<const uint8_t>(encoded, enc_len),
                                     etl::span<uint8_t>(decoded, 20));
  TEST_ASSERT(dec_len > 0);

  TEST_ASSERT_EQ_UINT(
      rpc::cobs::decode(etl::span<const uint8_t>(src, 0),
                        etl::span<uint8_t>(decoded, 20)),
      0);

  TEST_ASSERT_EQ_UINT(
      rpc::cobs::decode(etl::span<const uint8_t>(encoded, enc_len),
                        etl::span<uint8_t>(decoded, 1)),
      0);

  uint8_t long_src[300];
  memset(long_src, 0x42, 300);
  long_src[100] = 0;
  uint8_t long_enc[320];
  uint8_t long_dec[320];
  enc_len = rpc::cobs::encode(etl::span<const uint8_t>(long_src, 300),
                              etl::span<uint8_t>(long_enc, 320));
  if (enc_len > 0) {
    dec_len = rpc::cobs::decode(etl::span<const uint8_t>(long_enc, enc_len),
                                etl::span<uint8_t>(long_dec, 320));
    TEST_ASSERT_EQ_UINT(dec_len, 300);
  }
}

void test_frame_overflow() {
  printf("  -> test_frame_overflow\n");

  rpc::FrameParser parser;
  const uint16_t fake_payload_len = rpc::MAX_PAYLOAD_SIZE + 1;
  const size_t total = fake_payload_len + 9;

  etl::vector<uint8_t, 1100> buf;
  buf.resize(total);
  buf[0] = rpc::PROTOCOL_VERSION;
  rpc::write_u16_be(&buf[1], fake_payload_len);
  rpc::write_u16_be(&buf[3], 0x40);
  memset(&buf[5], 0xAA, fake_payload_len);

  etl::crc32 crc_calc;
  crc_calc.add(buf.data(), buf.data() + total - 4);
  rpc::write_u32_be(&buf[total - 4], crc_calc.value());

  auto result = parser.parse(etl::span<const uint8_t>(buf.data(), total));
  TEST_ASSERT(!result.has_value());
}

void test_command_router() {
  printf("  -> test_command_router\n");

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = 0x50;

  bridge::router::CommandMessage msg(&f, 0x50, false, true);
  TEST_ASSERT_EQ_UINT(msg.get_message_id(), 0x50);
  TEST_ASSERT(msg.requires_ack == true);
  TEST_ASSERT(msg.is_duplicate == false);
  TEST_ASSERT(msg.frame == &f);
}

void test_console_edge_cases() {
  printf("  -> test_console_edge_cases\n");
  TestStream stream;
  reset_env(stream);

  TEST_ASSERT_EQ_UINT(Console.available(), 0);

  auto ba = bridge::test::TestAccessor::create(Bridge);
  auto ca = bridge::test::ConsoleTestAccessor::create(Console);
  ba.setUnsynchronized();
  ca.clearTxBuffer();
  while (!ca.isTxBufferFull()) {
    ca.pushTxByte('X');
  }
  TEST_ASSERT_EQ_UINT(Console.write('Z'), 0);
}

void test_process_edge_cases() {
  printf("  -> test_process_edge_cases\n");
#if BRIDGE_ENABLE_PROCESS
  TestStream stream;
  reset_env(stream);

  auto pa = bridge::test::ProcessTestAccessor::create(Process);

  uint16_t sentinel = pa.popPendingPid();
  TEST_ASSERT_EQ_UINT(sentinel, rpc::RPC_INVALID_ID_SENTINEL);

  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setUnsynchronized();
  Process.runAsync("ls");
#endif
}

void test_link_sync_full() {
  printf("  -> test_link_sync_full\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  ba.clearSharedSecret();
  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  f.header.payload_length = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  memset(f.payload.data(), 0xAA, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  reset_env(stream);
  {
  auto ba2 = bridge::test::TestAccessor::create(Bridge);
  const uint8_t secret[] = "test_secret_key!";
  ba2.assignSharedSecret(secret, secret + 16);

  uint8_t nonce[rpc::RPC_HANDSHAKE_NONCE_LENGTH];
  memset(nonce, 0xBB, rpc::RPC_HANDSHAKE_NONCE_LENGTH);

  uint8_t tag[rpc::RPC_HANDSHAKE_TAG_LENGTH];
  ba2.computeHandshakeTag(nonce, rpc::RPC_HANDSHAKE_NONCE_LENGTH, tag);

  f.header.payload_length =
      rpc::RPC_HANDSHAKE_NONCE_LENGTH + rpc::RPC_HANDSHAKE_TAG_LENGTH;
  memcpy(f.payload.data(), nonce, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  memcpy(f.payload.data() + rpc::RPC_HANDSHAKE_NONCE_LENGTH, tag,
         rpc::RPC_HANDSHAKE_TAG_LENGTH);
  stream.tx.clear();
  ba2.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);
  TEST_ASSERT(Bridge.isSynchronized());
  }
}

void test_link_reset_with_config() {
  printf("  -> test_link_reset_with_config\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET);
  f.header.payload_length = rpc::payload::HandshakeConfig::SIZE;
  rpc::write_u16_be(f.payload.data(), 500);
  f.payload[2] = 3;
  rpc::write_u32_be(f.payload.data() + 3, 5000);
  ba.dispatch(f);
}

void test_dedup_with_ack() {
  printf("  -> test_dedup_with_ack\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  f.header.payload_length = rpc::payload::PinMode::SIZE;
  f.payload[0] = 13;
  f.payload[1] = 1;
  f.crc = 0x12345678;

  ba.dispatch(f);

  ba.setAckTimeoutMs(1000);
  ba.setAckRetryLimit(3);
  g_millis += 1500;

  ba.dispatch(f);
}

void test_status_null_handler() {
  printf("  -> test_status_null_handler\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_OK);
  f.header.payload_length = 0;
  ba.dispatch(f);

  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_ERROR);
  ba.dispatch(f);

  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_OVERFLOW);
  ba.dispatch(f);
}

void test_rpc_structs_encode() {
  printf("  -> test_rpc_structs_encode\n");

  uint8_t buf[128];

  rpc::payload::VersionResponse vr{1, 2};
  vr.encode(buf);
  TEST_ASSERT_EQ_UINT(buf[0], 1);
  TEST_ASSERT_EQ_UINT(buf[1], 2);

  rpc::payload::FreeMemoryResponse fmr{4096};
  fmr.encode(buf);

  rpc::payload::Capabilities cap{2, 0, 14, 6, 0x01};
  cap.encode(buf);

  rpc::payload::DigitalReadResponse drr{1};
  drr.encode(buf);

  rpc::payload::AnalogReadResponse arr{1023};
  arr.encode(buf);

  rpc::payload::SetBaudratePacket sbp{115200};
  sbp.encode(buf);

  rpc::payload::HandshakeConfig hc{500, 3, 5000};
  hc.encode(buf);

  rpc::payload::AckPacket ap{0x1234};
  ap.encode(buf);

  rpc::payload::PinMode pm{13, 1};
  pm.encode(buf);

  rpc::payload::DigitalWrite dw{13, 1};
  dw.encode(buf);

  rpc::payload::AnalogWrite aw{9, 128};
  aw.encode(buf);

  rpc::payload::PinRead pr{7};
  pr.encode(buf);

  rpc::payload::MailboxProcessed mp{1};
  mp.encode(buf);

  rpc::payload::MailboxAvailableResponse mar{42};
  mar.encode(buf);

  rpc::payload::ProcessKill pk{99};
  pk.encode(buf);

  rpc::payload::ProcessPoll pp{42};
  pp.encode(buf);

  rpc::payload::ProcessRunAsyncResponse prar{123};
  prar.encode(buf);
}

void test_retransmit_via_malformed() {
  printf("  -> test_retransmit_via_malformed\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  uint8_t payload[] = {13, 1};
  Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE, etl::span<const uint8_t>(payload, 2));
  TEST_ASSERT(ba.isAwaitingAck());

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED);
  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload.data(), ba.getLastCommandId());
  ba.dispatch(f);
}

void test_send_frame_critical_path() {
  printf("  -> test_send_frame_critical_path\n");
#if BRIDGE_ENABLE_DATASTORE
  TestStream stream;
  reset_env(stream);
  DataStore.requestGet("key1");
  DataStore.requestGet("key2");
#endif
}

void test_rpc_structs_parse_specializations() {
  printf("  -> test_rpc_structs_parse_specializations\n");

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  f.header.payload_length = 5;
  memcpy(f.payload.data(), "hello", 5);
  auto cw = rpc::Payload::parse<rpc::payload::ConsoleWrite>(f);
  TEST_ASSERT(cw.has_value());
  TEST_ASSERT_EQ_UINT(cw->length, 5);

  f.header.payload_length = 2;
  memcpy(f.payload.data(), "ls", 2);
  auto pra = rpc::Payload::parse<rpc::payload::ProcessRunAsync>(f);
  TEST_ASSERT(pra.has_value());

  f.header.payload_length = 4;
  rpc::write_u16_be(f.payload.data(), 2);
  f.payload[2] = 'A';
  f.payload[3] = 'B';
  auto mrr = rpc::Payload::parse<rpc::payload::MailboxReadResponse>(f);
  TEST_ASSERT(mrr.has_value());
  TEST_ASSERT_EQ_UINT(mrr->length, 2);

  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload.data(), 100);
  auto mrr2 = rpc::Payload::parse<rpc::payload::MailboxReadResponse>(f);
  TEST_ASSERT(!mrr2.has_value());
}

void test_status_handler_callback() {
  printf("  -> test_status_handler_callback\n");
}

void test_debug_io_log() {
  printf("  -> test_debug_io_log\n");
  TestStream stream;
  reset_env(stream);

  stream.tx.clear();
  Bridge.sendFrame(rpc::StatusCode::STATUS_OK);
  TEST_ASSERT(stream.tx.len > 0);
}

void test_flush_when_awaiting_ack() {
  printf("  -> test_flush_when_awaiting_ack\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  uint8_t payload[] = {13, 1};
  Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE, etl::span<const uint8_t>(payload, 2));
  TEST_ASSERT(ba.isAwaitingAck());

  Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE, etl::span<const uint8_t>(payload, 2));
  TEST_ASSERT(ba.isAwaitingAck());
}

}  // namespace

int main() {
  printf("COVERAGE GAPS TEST START\n");
  test_gpio_commands_via_dispatch();
  test_console_write_via_dispatch();
  test_datastore_resp_via_dispatch();
  test_mailbox_via_dispatch();
  test_filesystem_via_dispatch();
  test_process_via_dispatch();
  test_unknown_command_via_dispatch();
  test_system_commands_via_dispatch();
  test_ack_and_retransmit();
  test_ack_timeout_with_status_handler();
  test_timer_callbacks();
  test_fsm_transitions();
  test_observer_notifications();
  test_bridge_events_defaults();
  test_hal_free_memory();
  test_apply_timing_config();
  test_send_key_val_command();
  test_emit_status();
  test_emit_status_with_observer();
  test_cobs_overflow_in_process();
  test_cobs_decode();
  test_frame_overflow();
  test_command_router();
  test_console_edge_cases();
  test_process_edge_cases();
  test_link_sync_full();
  test_link_reset_with_config();
  test_dedup_with_ack();
  test_status_null_handler();
  test_rpc_structs_encode();
  test_retransmit_via_malformed();
  test_send_frame_critical_path();
  test_rpc_structs_parse_specializations();
  test_status_handler_callback();
  test_debug_io_log();
  test_flush_when_awaiting_ack();
  printf("COVERAGE GAPS TEST END\n");
  return 0;
}
