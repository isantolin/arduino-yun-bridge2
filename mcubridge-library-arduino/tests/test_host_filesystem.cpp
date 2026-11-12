#define BRIDGE_ENABLE_TEST_INTERFACE 1

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif
#include <etl/array.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "services/FileSystem.h"
#include "test_support.h"

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

namespace {
using bridge::test::TestAccessor;

void test_hal_roundtrip() {
  etl::array<uint8_t, 8> payload = {'m', 'c', 'u', '-', 'd', 'a', 't', 'a'};
  const etl::string_view path = "test_hal.bin";

  auto res_w = bridge::hal::writeFile(
      path, pw::span<const uint8_t>(payload.data(), payload.size()));
  TEST_ASSERT(res_w.ok());

  etl::array<uint8_t, 32> read_buffer;
  read_buffer.fill(0);
  auto res_r = bridge::hal::readFileChunk(
      path, 0, pw::span<uint8_t>(read_buffer.data(), read_buffer.size()));
  TEST_ASSERT(res_r.ok());
  TEST_ASSERT_EQUAL(payload.size(), res_r->bytes_read);
  TEST_ASSERT(etl::equal(payload.begin(), payload.end(), read_buffer.begin()));

  TEST_ASSERT_TRUE(bridge::hal::removeFile(path).ok());
}

void test_hal_chunked_read_roundtrip() {
  const etl::string_view path = "test_chunks.bin";
  etl::array<uint8_t, 120> read_payload;
  size_t idx = 0;
  etl::generate(read_payload.begin(), read_payload.end(),
                [&idx]() { return static_cast<uint8_t>('a' + (idx++ % 26U)); });

  auto res_w = bridge::hal::writeFile(
      path, pw::span<const uint8_t>(read_payload.data(), read_payload.size()));
  TEST_ASSERT(res_w.ok());

  etl::array<uint8_t, 62> first_chunk;
  first_chunk.fill(0);
  etl::array<uint8_t, 62> second_chunk;
  second_chunk.fill(0);

  auto res_r1 = bridge::hal::readFileChunk(
      path, 0, pw::span<uint8_t>(first_chunk.data(), first_chunk.size()));
  TEST_ASSERT(res_r1.ok());
  TEST_ASSERT_EQUAL(62, res_r1->bytes_read);
  TEST_ASSERT(res_r1->has_more);

  auto res_r2 = bridge::hal::readFileChunk(
      path, 62, pw::span<uint8_t>(second_chunk.data(), second_chunk.size()));
  TEST_ASSERT(res_r2.ok());
  TEST_ASSERT_EQUAL(120 - 62, res_r2->bytes_read);
  TEST_ASSERT(!res_r2->has_more);

  TEST_ASSERT(etl::equal(read_payload.begin(), read_payload.begin() + 62,
                         first_chunk.begin()));
  TEST_ASSERT(etl::equal(read_payload.begin() + 62, read_payload.end(),
                         second_chunk.begin()));

  TEST_ASSERT_TRUE(bridge::hal::removeFile(path).ok());
}

void test_filesystem_api_write() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  etl::array<uint8_t, 3> data = {1, 2, 3};
  FileSystem.write("api_write.bin",
                   pw::span<const uint8_t>(data.data(), data.size()));
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
  TEST_ASSERT_TRUE(stream.tx_buf.len > 0);
}

void test_filesystem_api_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  FileSystem.remove("api_rem.bin");
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
  TEST_ASSERT_TRUE(stream.tx_buf.len > 0);
}

void test_filesystem_on_write() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  etl::array<uint8_t, 3> resp_data = {4, 5, 6};
  rpc_pb_FileWrite msg = rpc_pb_FileWrite_init_default;
  bridge::pb::copy_string(msg.path, sizeof(msg.path), "on_write.bin");
  bridge::pb::copy_bytes(msg.data, resp_data.data(), resp_data.size());
  FileSystem._onWrite(msg);
  TEST_ASSERT_TRUE(bridge::hal::removeFile("on_write.bin").ok());
}

void test_filesystem_on_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  etl::array<uint8_t, 2> dummy = {1, 2};
  TEST_ASSERT_TRUE(
      bridge::hal::writeFile(
          "on_rem.bin", pw::span<const uint8_t>(dummy.data(), dummy.size()))
          .ok());
  rpc_pb_FileRemove msg = rpc_pb_FileRemove_init_default;
  bridge::pb::copy_string(msg.path, sizeof(msg.path), "on_rem.bin");
  FileSystem._onRemove(msg);
  TEST_ASSERT_FALSE(bridge::hal::removeFile("on_rem.bin").ok());
}

void test_filesystem_api_empty_and_error_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Empty write
  FileSystem.write("", pw::span<const uint8_t>());

  // Empty remove
  FileSystem.remove("");
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_hal_roundtrip);
  RUN_TEST(test_hal_chunked_read_roundtrip);
  RUN_TEST(test_filesystem_api_write);
  RUN_TEST(test_filesystem_api_remove);
  RUN_TEST(test_filesystem_on_write);
  RUN_TEST(test_filesystem_on_remove);
  RUN_TEST(test_filesystem_api_empty_and_error_paths);
  return UNITY_END();
}
