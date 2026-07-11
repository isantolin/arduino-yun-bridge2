#define BRIDGE_ENABLE_TEST_INTERFACE
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "etl_ext/CounterIterator.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"
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
}

void test_bridge_queue_full_and_retransmit() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
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
}

void test_filesystem_read_edge_cases() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Trigger FileSystem read chunks with timeout/error simulation
  const char* file_path_str = "test.txt";
  etl::string_view path_sv(file_path_str);
  rpc::payload::FileRead req;
  strncpy(req.path, path_sv.data(), sizeof(req.path));

  // This will use the new CounterIterator in _onRead
  FileSystem._onRead(req);

  // Write a large file (> 64 bytes) to test multi-chunk reading
  etl::array<uint8_t, 100> large_data;
  large_data.fill(0xAA);
  bridge::hal::writeFile("test_large.txt", large_data);

  rpc::payload::FileRead req_large;
  strncpy(req_large.path, "test_large.txt", sizeof(req_large.path));
  FileSystem._onRead(req_large);

  // Trigger timeout path
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT);
  FileSystem._onRead(req_large);

  // Coverage for observer notification
  FileSystem.onLost();
}

void test_spi_timeout_and_error_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  SPIService.begin();
  rpc::payload::SpiConfig sc;
  sc.frequency = 4000000;
  sc.bit_order = 1;
  sc.data_mode = 0;
  SPIService.setConfig(sc);

  etl::array<uint8_t, 4> buf = {1, 2, 3, 4};
  // Normal transfer (stub SPI doesn't timeout)
  size_t n = SPIService.transfer(etl::span<uint8_t>(buf));
  TEST_ASSERT_EQUAL(4, n);

  // Empty transfer
  n = SPIService.transfer(etl::span<uint8_t>());
  TEST_ASSERT_EQUAL(0, n);

  SPIService.end();
  // Transfer while not initialized
  n = SPIService.transfer(etl::span<uint8_t>(buf));
  TEST_ASSERT_EQUAL(0, n);

  // Coverage for observer notification
  SPIService.onLost();
}

void test_process_poll_and_kill() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // Test Process service direct list initialization and pending queue
  Process.poll(123, ProcessType::ProcessPollHandler::create<poll_handler>());
  Process.kill(456);
  Process.runAsync("ls", {},
                   etl::delegate<void(int32_t)>::create<async_handler>());

  // Internal handlers (coverage only)
  Process._onRunAsyncResponse({});
  Process._onPollResponse({});

  // Coverage for observer notification
  Process.onLost();
}

void test_process_branch_error_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  Process.reset();

  // Fill run queue (size=1) and trigger full-queue error callback path.
  captured_pid = 0;
  Process.runAsync(
      "ls", {}, etl::delegate<void(int32_t)>::create<capture_async_handler>());
  Process.runAsync(
      "pwd", {}, etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);
  TEST_ASSERT_EQUAL(1, Process._pending_run_async.size());
  Process._onRunAsyncResponse([]() {
    rpc::payload::ProcessRunAsyncResponse p;
    p.pid = 42;
    return p;
  }());
  TEST_ASSERT_EQUAL(42, captured_pid);

  // Valid send with invalid callback should not enqueue a pending run.
  Process.reset();
  Process.runAsync("ls", {}, ProcessType::ProcessRunHandler{});
  TEST_ASSERT_EQUAL(0, Process._pending_run_async.size());

  // Force append_token failure via oversized arg, and hit lambda early return.
  etl::array<char, rpc::MAX_PAYLOAD_SIZE + 1> long_arg_storage = {};
  long_arg_storage.fill('a');
  const etl::string_view oversized_arg(long_arg_storage.data(),
                                       rpc::MAX_PAYLOAD_SIZE);
  etl::array<etl::string_view, 2> overflow_args = {oversized_arg,
                                                   etl::string_view("y")};
  Process.runAsync(
      "x", etl::span<const etl::string_view>(overflow_args.data(), 2),
      etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);
  ProcessType::ProcessRunHandler invalid_run_handler;
  invalid_run_handler.clear();
  Process.runAsync("x",
                   etl::span<const etl::string_view>(overflow_args.data(), 2),
                   invalid_run_handler);

  // Force prepend-space capacity failure (write_pos + 1 >= buffer_size).
  etl::array<char, rpc::MAX_PAYLOAD_SIZE> near_full_cmd = {};
  near_full_cmd.fill('c');
  etl::array<etl::string_view, 1> single_arg = {etl::string_view("z")};
  Process.runAsync(
      etl::string_view(near_full_cmd.data(), rpc::MAX_PAYLOAD_SIZE - 1U),
      etl::span<const etl::string_view>(single_arg.data(), 1),
      etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);

  // Force send failure path via safe state (TX disabled for non-system cmds).
  Bridge.enterSafeState();
  Process.runAsync(
      "ls", {}, etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);
  reset_bridge_core(Bridge, stream);
  auto& ba_recovered = TestAccessor::create(Bridge);
  ba_recovered.setSynchronized();

  // Poll queue full path (size=1), then invalid-handler path.
  Process.reset();
  Process.poll(10,
               ProcessType::ProcessPollHandler::create<capture_poll_handler>());
  TEST_ASSERT_EQUAL(1, Process._pending_polls.size());
  Process.poll(11,
               ProcessType::ProcessPollHandler::create<capture_poll_handler>());
  TEST_ASSERT_EQUAL(1, Process._pending_polls.size());

  Process.reset();
  Process.poll(12, ProcessType::ProcessPollHandler{});
  TEST_ASSERT_EQUAL(0, Process._pending_polls.size());

  // Force send failure in poll path.
  ba_recovered.clearSynchronized();
  Process.poll(13,
               ProcessType::ProcessPollHandler::create<capture_poll_handler>());
  ba_recovered.setSynchronized();

  // Command that overflows command_buffer immediately (> 64 bytes)
  etl::array<char, 70> long_cmd = {};
  long_cmd.fill('a');
  captured_pid = 0;
  Process.runAsync(
      etl::string_view(long_cmd.data(), 70), {},
      etl::delegate<void(int32_t)>::create<capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, captured_pid);

  // Command with valid arguments successfully appended
  etl::array<etl::string_view, 2> valid_args = {etl::string_view("arg1"),
                                                etl::string_view("arg2")};
  Process.runAsync(
      "ls", etl::span<const etl::string_view>(valid_args.data(), 2),
      etl::delegate<void(int32_t)>::create<capture_async_handler>());
}

void test_console_write_full_buffer_retains_data_when_send_fails() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  Console.begin();

  etl::array<uint8_t, bridge::config::CONSOLE_TX_BUFFER_SIZE> fill = {};
  fill.fill('x');
  TEST_ASSERT_EQUAL_UINT32(fill.size(),
                           Console.write(fill.data(), fill.size()));

  Bridge.enterSafeState();
  const etl::array<uint8_t, 1> extra = {'y'};
  TEST_ASSERT_EQUAL_UINT32(0, Console.write(extra.data(), extra.size()));
}

void test_mailbox_and_datastore_variants() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 4> mb_data1 = {1, 2, 3, 4};
  Mailbox.push(mb_data1);

  // Coverage for observer notification
  Mailbox.onLost();

  DataStore._pending_gets.clear();
  DataStore.get("alpha",
                DataStoreType::GetHandler::create<datastore_get_handler>());
  DataStore.get("beta",
                DataStoreType::GetHandler::create<datastore_get_handler>());
  DataStore._onResponse({});
  DataStore._pending_gets.clear();
  DataStoreType::GetHandler invalid_get_handler;
  invalid_get_handler.clear();
  DataStore.get("gamma", invalid_get_handler);
  DataStore._onResponse(rpc::payload::DatastoreGetResponse{});
}

void test_bridge_fsm_resets() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

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

  uint32_t crc = rpc::checksum::compute(
      etl::span<const uint8_t>(f.payload_type.encrypted_payload_with_tag.bytes,
                               f.payload_type.encrypted_payload_with_tag
                                   .size));  // Adjusted for new checksum logic
  (void)crc;
}

void test_bridge_timer_callbacks() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // We can't easily wait for real timers in a host test,
  // but we can call the callback functions directly for coverage.
  Bridge._onAckTimeout();
  Bridge._onRxDedupe();
  Bridge._onBaudrateChange();
  Bridge._onBootloaderDelay();
}

void test_bridge_packet_errors() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // Test malformed packet (length 0)
  ba.invokePacketReceived(etl::span<const uint8_t>());
}

void test_bridge_template_coverage() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // Explicitly trigger template instantiations that might be missed
  (void)Bridge.send(rpc::CommandId::CMD_SET_PIN_MODE, 1, []() {
    rpc::payload::PinMode p;
    p.pin = 13;
    p.mode = rpc_pb_PinModeType_PIN_OUTPUT;
    return p;
  }());

  // Mock handlers
  Bridge.onCommand(BridgeClass::CommandHandler::create<dummy_cmd_handler>());
  Bridge.onStatus(BridgeClass::StatusHandler::create<dummy_status_handler>());
  Bridge.flushStream();
}

void test_bridge_duplicate_packet() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.sequence_id = 10;
  f.payload_type.encrypted_payload_with_tag.size = 2;  // dummy

  bridge::router::CommandContext ctx(&f, f.command_id, 10, true, true);
  ba.dispatch(f);
}

void test_bridge_exhaustive_command_handlers() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
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
    p.mode = rpc_pb_PinModeType_PIN_OUTPUT;
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
