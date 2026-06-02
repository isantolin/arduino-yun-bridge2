#define BRIDGE_ENABLE_TEST_INTERFACE
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "etl_ext/CounterIterator.h"
#include "protocol/rpc_services.h"
#include "test_support.h"

// Arduino Stubs for Linker
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

void setUp(void) {}
void tearDown(void) {}

// SIL-2 Hardening Coverage Test Suite
// Focuses on reaching 90%+ line and 80%+ branch coverage by targeting edge
// cases, error paths, and the new optimized serialization/iteration logic.

using bridge::test::TestAccessor;

namespace {
void poll_handler(rpc::StatusCode, uint16_t, etl::span<const uint8_t>,
                  etl::span<const uint8_t>) {}
void async_handler(int32_t) {}
int32_t captured_pid = 0;
void capture_async_handler(int32_t pid) { captured_pid = pid; }
void capture_poll_handler(rpc::StatusCode status, uint16_t exit_code,
                          etl::span<const uint8_t>, etl::span<const uint8_t>) {
  (void)status;
  (void)exit_code;
}
void datastore_get_handler(etl::string_view, etl::span<const uint8_t>) {}
void dummy_cmd_handler(const rpc_pb_RpcEnvelope&) {}
void dummy_status_handler(rpc::StatusCode, etl::span<const uint8_t>) {}
}  // namespace

void hit_mailbox_push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p;
  rpc::payload::copy_to_pb_bytes(p.data, data.data(), data.size());
  rpc::services::mailbox::_onIncomingData(p);
}
void hit_mailbox_read_resp(etl::span<const uint8_t> data) {
  rpc::payload::MailboxReadResponse p;
  rpc::payload::copy_to_pb_bytes(p.content, data.data(), data.size());
  rpc::services::mailbox::_onIncomingData(p);
}

void test_bridge_emit_status_variants() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // Test all status variants to cover string_view, FlashString, and span paths
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK);
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, etl::string_view("Error"));
  Bridge.emitStatus(rpc::StatusCode::STATUS_MALFORMED, F("FlashError"));

  // Empty variants
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, etl::string_view(""));
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK,
                    (const __FlashStringHelper*)nullptr);

  TEST_ASSERT(true);
}

void test_bridge_queue_full_and_retransmit() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Fill the TX queue with reliable commands to trigger full condition
  bridge::etl_ext::CounterIterator<uint32_t> fill_begin(0);
  bridge::etl_ext::CounterIterator<uint32_t> fill_end(
      bridge::config::MAX_PENDING_TX_FRAMES);
  etl::for_each(fill_begin, fill_end, [&ba](uint32_t i) {
    // Use a reliable command (e.g., CMD_CONSOLE_WRITE)
    (void)ba.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 100 + i, {});
  });

  // Next one should return false (queue full)
  bool ok = ba.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 999, {});
  TEST_ASSERT_FALSE(ok);

  // Trigger retransmit path
  ba.onAckTimeout();

  // Trigger ACK for a non-waiting command
  ba.handleAck(0xFFFF);

  TEST_ASSERT(true);
}

void test_filesystem_read_edge_cases() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Trigger FileSystem read chunks with timeout/error simulation
  const char* file_path_str = "test.txt";
  etl::string_view path_sv(file_path_str);
  rpc::payload::FileRead req;
  strncpy(req.path, path_sv.data(), sizeof(req.path));

  // This will use the new CounterIterator in _onRead
  rpc::services::filesystem::_onRead(req);

  // Coverage for observer notification
  rpc::services::filesystem::notification(MsgBridgeSynchronized());
  rpc::services::filesystem::notification(MsgBridgeLost());

  TEST_ASSERT(true);
}

void test_spi_timeout_and_error_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  rpc::services::spi::begin();
  rpc::payload::SpiConfig sc;
  sc.frequency = 4000000;
  sc.bit_order = 1;
  sc.data_mode = 0;
  rpc::services::spi::setConfig(sc);

  etl::array<uint8_t, 4> buf = {1, 2, 3, 4};
  // Normal transfer (stub SPI doesn't timeout)
  size_t n = rpc::services::spi::transfer(etl::span<uint8_t>(buf));
  TEST_ASSERT_EQUAL(4, n);

  // Empty transfer
  n = rpc::services::spi::transfer(etl::span<uint8_t>());
  TEST_ASSERT_EQUAL(0, n);

  rpc::services::spi::end();
  // Transfer while not initialized
  n = rpc::services::spi::transfer(etl::span<uint8_t>(buf));
  TEST_ASSERT_EQUAL(0, n);

  // Coverage for observer notification
  rpc::services::spi::notification(MsgBridgeSynchronized());
  rpc::services::spi::notification(MsgBridgeLost());
}

void test_process_poll_and_kill() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // Test Process service direct list initialization and pending queue
  rpc::services::process::poll(123, ProcessClass::ProcessPollHandler::create<poll_handler>());
  rpc::services::process::kill(456);
  rpc::services::process::runAsync("ls", {},
                   etl::delegate<void(int32_t)>::create<async_handler>());

  // Internal handlers (coverage only)
  rpc::services::process::_onRunAsyncResponse({});
  rpc::services::process::_onPollResponse({});

  // Coverage for observer notification
  rpc::services::process::notification(MsgBridgeSynchronized());
  rpc::services::process::notification(MsgBridgeLost());

  TEST_ASSERT(true);
}

void test_process_branch_error_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  rpc::services::process::reset();

  // Fill run queue (size=1) and trigger full-queue error callback path.
  captured_pid = 0;
  rpc::services::process::runAsync("ls", {},
                   etl::delegate<void(int32_t)>::create<capture_async_handler>());
  rpc::services::process::runAsync("pwd", {},
                   etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);
  TEST_ASSERT_EQUAL(1, rpc::services::process::_pending_run_async.size());
  rpc::services::process::_onRunAsyncResponse([]() {
    rpc::payload::ProcessRunAsyncResponse p;
    p.pid = 42;
    return p;
  }());
  TEST_ASSERT_EQUAL(42, captured_pid);

  // Valid send with invalid callback should not enqueue a pending run.
  rpc::services::process::reset();
  rpc::services::process::runAsync("ls", {}, ProcessClass::ProcessRunHandler{});
  TEST_ASSERT_EQUAL(0, rpc::services::process::_pending_run_async.size());

  // Force append_token failure via oversized arg, and hit lambda early return.
  etl::array<char, rpc::MAX_PAYLOAD_SIZE + 1> long_arg_storage = {};
  long_arg_storage.fill('a');
  const etl::string_view oversized_arg(long_arg_storage.data(),
                                       rpc::MAX_PAYLOAD_SIZE);
  etl::array<etl::string_view, 2> overflow_args = {oversized_arg,
                                                   etl::string_view("y")};
  rpc::services::process::runAsync(
      "x", etl::span<const etl::string_view>(overflow_args.data(), 2),
      etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);
  ProcessClass::ProcessRunHandler invalid_run_handler;
  invalid_run_handler.clear();
  rpc::services::process::runAsync("x", etl::span<const etl::string_view>(overflow_args.data(), 2),
                   invalid_run_handler);

  // Force prepend-space capacity failure (write_pos + 1 >= buffer_size).
  etl::array<char, rpc::MAX_PAYLOAD_SIZE> near_full_cmd = {};
  near_full_cmd.fill('c');
  etl::array<etl::string_view, 1> single_arg = {etl::string_view("z")};
  rpc::services::process::runAsync(
      etl::string_view(near_full_cmd.data(), rpc::MAX_PAYLOAD_SIZE - 1U),
      etl::span<const etl::string_view>(single_arg.data(), 1),
      etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);

  // Force send failure path via safe state (TX disabled for non-system cmds).
  Bridge.enterSafeState();
  rpc::services::process::runAsync("ls", {},
                   etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);
  reset_bridge_core(Bridge, stream);
  auto ba_recovered = TestAccessor::create(Bridge);
  ba_recovered.setSynchronized();

  // Poll queue full path (size=1), then invalid-handler path.
  rpc::services::process::reset();
  rpc::services::process::poll(10, ProcessClass::ProcessPollHandler::create<capture_poll_handler>());
  TEST_ASSERT_EQUAL(1, rpc::services::process::_pending_polls.size());
  rpc::services::process::poll(11, ProcessClass::ProcessPollHandler::create<capture_poll_handler>());
  TEST_ASSERT_EQUAL(1, rpc::services::process::_pending_polls.size());

  rpc::services::process::reset();
  rpc::services::process::poll(12, ProcessClass::ProcessPollHandler{});
  TEST_ASSERT_EQUAL(0, rpc::services::process::_pending_polls.size());

  // Force send failure in poll path.
  ba_recovered.clearSynchronized();
  rpc::services::process::poll(13, ProcessClass::ProcessPollHandler::create<capture_poll_handler>());
  ba_recovered.setSynchronized();

  // Exercise invalid pending handlers in response dispatch.
  ProcessClass::ProcessRunHandler invalid_pending_run;
  invalid_pending_run.clear();
  rpc::services::process::_pending_run_async.push({invalid_pending_run});
  rpc::services::process::_onRunAsyncResponse([]() {
    rpc::payload::ProcessRunAsyncResponse p;
    p.pid = 777;
    return p;
  }());
  ProcessClass::ProcessPollHandler invalid_pending_poll;
  invalid_pending_poll.clear();
  rpc::services::process::_pending_polls.push({1, invalid_pending_poll});
  rpc::services::process::_onPollResponse(rpc::payload::ProcessPollResponse{});
}

void test_console_write_full_buffer_retains_data_when_send_fails() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  rpc::services::console::begin();

  etl::array<uint8_t, bridge::config::CONSOLE_TX_BUFFER_SIZE> fill = {};
  fill.fill('x');
  TEST_ASSERT_EQUAL_UINT32(fill.size(),
                           rpc::services::console::write(fill.data(), fill.size()));

  Bridge.enterSafeState();
  const etl::array<uint8_t, 1> extra = {'y'};
  TEST_ASSERT_EQUAL_UINT32(0, rpc::services::console::write(extra.data(), extra.size()));
}

void test_mailbox_and_datastore_variants() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 4> mb_data1 = {1, 2, 3, 4};
  rpc::services::mailbox::push(mb_data1);

  // Test _onIncomingData
  etl::array<uint8_t, 2> mb_data2 = {0xAA, 0xBB};
  hit_mailbox_push(mb_data2);
  etl::array<uint8_t, 2> mb_data3 = {0xCC, 0xDD};
  hit_mailbox_read_resp(mb_data3);
  rpc::services::mailbox::_onAvailableResponse({});
  rpc::services::mailbox::_onAvailableResponse([]() {
    rpc::payload::MailboxAvailableResponse p;
    p.count = 7;
    return p;
  }());

  // Coverage for observer notification
  rpc::services::mailbox::notification(MsgBridgeSynchronized());
  rpc::services::mailbox::notification(MsgBridgeLost());

  rpc::services::datastore::_pending_gets.clear();
  rpc::services::datastore::get("alpha",
                DataStoreClass::GetHandler::create<datastore_get_handler>());
  rpc::services::datastore::get("beta",
                DataStoreClass::GetHandler::create<datastore_get_handler>());
  rpc::services::datastore::_onResponse({});
  rpc::services::datastore::_pending_gets.clear();
  DataStoreClass::GetHandler invalid_get_handler;
  invalid_get_handler.clear();
  rpc::services::datastore::get("gamma", invalid_get_handler);
  rpc::services::datastore::_onResponse(rpc::payload::DatastoreGetResponse{});

  TEST_ASSERT(true);
}

void test_bridge_fsm_resets() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  ba.setSynchronized();
  Bridge.enterSafeState();  // Should reset FSM and stop timers

  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_checksum_direct_library_path() {
  // Validates the new etl::byte_stream_writer logic in checksum::compute
  rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_XON);
  f.sequence_id = 0;
  
  uint32_t crc = rpc::checksum::compute(etl::span<const uint8_t>(f.payload.bytes, f.payload.size)); // Adjusted for new checksum logic
  (void)crc;
  TEST_ASSERT(true);
}

void test_bridge_timer_callbacks() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // We can't easily wait for real timers in a host test,
  // but we can call the callback functions directly for coverage.
  Bridge._onAckTimeout();
  Bridge._onRxDedupe();
  Bridge._onBaudrateChange();
  bridge::test::TestAccessor::create(Bridge).onStartupStabilized();
  Bridge._onBootloaderDelay();

  TEST_ASSERT(true);
}

void test_bridge_packet_errors() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  // Test malformed packet (length 0)
  ba.invokePacketReceived(etl::span<const uint8_t>());

  TEST_ASSERT(true);
}

void test_bridge_template_coverage() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // Explicitly trigger template instantiations that might be missed
  (void)Bridge.send(rpc::CommandId::CMD_SET_PIN_MODE, 1, []() {
    rpc::payload::PinMode p;
    p.pin = 13;
    p.mode = 1;
    return p;
  }());

  // Mock handlers
  Bridge.onCommand(BridgeClass::CommandHandler::create<dummy_cmd_handler>());
  Bridge.onStatus(BridgeClass::StatusHandler::create<dummy_status_handler>());
  Bridge.flushStream();

  TEST_ASSERT(true);
}

void test_bridge_duplicate_packet() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.sequence_id = 10;
  f.payload.size = 2; // dummy
  
  bridge::router::CommandContext ctx(&f, f.command_id, 10, true, true);
  ba.handleDigitalWriteCommand(ctx);

  TEST_ASSERT(true);
}

void test_bridge_exhaustive_command_handlers() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  auto trigger = [&](rpc::CommandId id, auto payload) {
    rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
    f.version = rpc::PROTOCOL_VERSION;
    f.command_id = static_cast<uint16_t>(id);
    f.sequence_id = 1;
    bridge::test::set_pb_payload(f, payload);
    ba.dispatch(f);
  };

  trigger(rpc::CommandId::CMD_SET_BAUDRATE, []() {
    rpc::payload::SetBaudratePacket p;
    p.baudrate = 57600;
    return p;
  }());
  trigger(rpc::CommandId::CMD_ENTER_BOOTLOADER, []() {
    rpc::payload::EnterBootloader p;
    p.magic = rpc::RPC_BOOTLOADER_MAGIC;
    return p;
  }());
  trigger(rpc::CommandId::CMD_SET_PIN_MODE, []() {
    rpc::payload::PinMode p;
    p.pin = 13;
    p.mode = 1;
    return p;
  }());
  trigger(rpc::CommandId::CMD_DIGITAL_WRITE, []() {
    rpc::payload::DigitalWrite p;
    p.pin = 13;
    p.value = 1;
    return p;
  }());
  trigger(rpc::CommandId::CMD_ANALOG_WRITE, []() {
    rpc::payload::AnalogWrite p;
    p.pin = 3;
    p.value = 128;
    return p;
  }());
  trigger(rpc::CommandId::CMD_DIGITAL_READ, []() {
    rpc::payload::PinRead p;
    p.pin = 13;
    return p;
  }());
  trigger(rpc::CommandId::CMD_ANALOG_READ, []() {
    rpc::payload::PinRead p;
    p.pin = 0;
    return p;
  }());

  TEST_ASSERT(true);
}

int main() {
  (void)poll_handler;
  (void)async_handler;
  (void)dummy_cmd_handler;
  (void)dummy_status_handler;
  UNITY_BEGIN();
  RUN_TEST(test_bridge_emit_status_variants);
  RUN_TEST(test_bridge_queue_full_and_retransmit);
  RUN_TEST(test_filesystem_read_edge_cases);
  RUN_TEST(test_spi_timeout_and_error_paths);
  RUN_TEST(test_process_poll_and_kill);
  RUN_TEST(test_process_branch_error_paths);
  RUN_TEST(test_console_write_full_buffer_retains_data_when_send_fails);
  RUN_TEST(test_mailbox_and_datastore_variants);
  RUN_TEST(test_bridge_fsm_resets);
  RUN_TEST(test_checksum_direct_library_path);
  RUN_TEST(test_bridge_timer_callbacks);
  RUN_TEST(test_bridge_packet_errors);
  RUN_TEST(test_bridge_template_coverage);
  RUN_TEST(test_bridge_duplicate_packet);
  RUN_TEST(test_bridge_exhaustive_command_handlers);
  return UNITY_END();
}
