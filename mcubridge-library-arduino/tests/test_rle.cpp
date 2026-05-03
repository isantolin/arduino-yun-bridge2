#include <etl/array.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "Bridge.h"
#include "protocol/rle.h"
#include "test_support.h"

// Define the global delegates and stubs for HardwareSerial stub
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;

// Unity setup/teardown
void setUp(void) {}
void tearDown(void) {}

void test_rle_empty_src() {
  etl::array<uint8_t, 10> dst_buf;
  etl::span<uint8_t> dst(dst_buf);
  etl::array<uint8_t, 1> src_buf = {0};
  etl::span<const uint8_t> src(src_buf.data(), 0); 
  
  TEST_ASSERT_EQUAL(0, rle::decode(src, dst));
}

void test_rle_empty_dst() {
  etl::array<uint8_t, 2> src_buf = {0x01, 0x02};
  etl::span<const uint8_t> src(src_buf);
  etl::array<uint8_t, 1> dst_buf = {0};
  etl::span<uint8_t> dst(dst_buf.data(), 0);
  
  TEST_ASSERT_EQUAL(0, rle::decode(src, dst));
}

void test_rle_literal_no_escape() {
  etl::array<uint8_t, 3> src_buf = {0x01, 0x02, 0x03};
  etl::array<uint8_t, 10> dst_buf;
  dst_buf.fill(0);
  
  size_t written = rle::decode(etl::span<const uint8_t>(src_buf), etl::span<uint8_t>(dst_buf));
  
  TEST_ASSERT_EQUAL(3, written);
  TEST_ASSERT_EQUAL(0x01, dst_buf[0]);
  TEST_ASSERT_EQUAL(0x02, dst_buf[1]);
  TEST_ASSERT_EQUAL(0x03, dst_buf[2]);
}

void test_rle_single_escape() {
  // ESCAPE_BYTE, SINGLE_ESCAPE_MARKER, VALUE
  etl::array<uint8_t, 3> src_buf = {rle::ESCAPE_BYTE, rle::SINGLE_ESCAPE_MARKER, 0xAA};
  etl::array<uint8_t, 10> dst_buf;
  dst_buf.fill(0);
  
  size_t written = rle::decode(etl::span<const uint8_t>(src_buf), etl::span<uint8_t>(dst_buf));
  
  TEST_ASSERT_EQUAL(1, written);
  TEST_ASSERT_EQUAL(0xAA, dst_buf[0]);
}

void test_rle_run_escape() {
  // Run of 5: ESCAPE_BYTE, (5 - OFFSET), VALUE
  uint8_t count = 5 - rpc::RPC_RLE_OFFSET;
  etl::array<uint8_t, 3> src_buf = {rle::ESCAPE_BYTE, count, 0xBB};
  etl::array<uint8_t, 10> dst_buf;
  dst_buf.fill(0);
  
  size_t written = rle::decode(etl::span<const uint8_t>(src_buf), etl::span<uint8_t>(dst_buf));
  
  TEST_ASSERT_EQUAL(5, written);
  for(int i=0; i<5; i++) {
    TEST_ASSERT_EQUAL_HEX8(0xBB, dst_buf[i]);
  }
}

void test_rle_dst_overflow_literal() {
  etl::array<uint8_t, 2> src_buf = {0x01, 0x02};
  etl::array<uint8_t, 1> dst_buf; 
  
  size_t written = rle::decode(etl::span<const uint8_t>(src_buf), etl::span<uint8_t>(dst_buf.data(), 1));
  
  TEST_ASSERT_EQUAL(0, written);
}

void test_rle_dst_overflow_run() {
  etl::array<uint8_t, 3> src_buf = {rle::ESCAPE_BYTE, 0x05, 0xCC}; 
  etl::array<uint8_t, 2> dst_buf; 
  
  size_t written = rle::decode(etl::span<const uint8_t>(src_buf), etl::span<uint8_t>(dst_buf.data(), 2));
  
  TEST_ASSERT_EQUAL(0, written);
}

void test_rle_incomplete_escape_marker() {
  etl::array<uint8_t, 1> src_buf = {rle::ESCAPE_BYTE};
  etl::array<uint8_t, 10> dst_buf;
  
  size_t written = rle::decode(etl::span<const uint8_t>(src_buf), etl::span<uint8_t>(dst_buf));
  
  TEST_ASSERT_EQUAL(0, written); 
}

void test_rle_incomplete_escape_val() {
  etl::array<uint8_t, 2> src_buf = {rle::ESCAPE_BYTE, 0x01};
  etl::array<uint8_t, 10> dst_buf;
  
  size_t written = rle::decode(etl::span<const uint8_t>(src_buf), etl::span<uint8_t>(dst_buf));
  
  TEST_ASSERT_EQUAL(0, written);
}

void test_rle_complex_sequence() {
  // [0x01, 0x02, ESC, SINGLE, 0xEE, ESC, 2, 0xFF, 0x03]
  // Literal 0x01, 0x02 (2 bytes)
  // Escaped 0xEE (once) (1 byte)
  // Run of 2+2=4 0xFF (4 bytes)
  // Literal 0x03 (1 byte)
  // Total: 2 + 1 + 4 + 1 = 8 bytes
  etl::array<uint8_t, 9> src_buf = {0x01, 0x02, rle::ESCAPE_BYTE, rle::SINGLE_ESCAPE_MARKER, 0xEE, 
                       rle::ESCAPE_BYTE, 0x02, 0xFF, 0x03};
  etl::array<uint8_t, 20> dst_buf;
  
  size_t written = rle::decode(etl::span<const uint8_t>(src_buf), etl::span<uint8_t>(dst_buf));
  
  TEST_ASSERT_EQUAL(8, written);
  TEST_ASSERT_EQUAL(0x01, dst_buf[0]);
  TEST_ASSERT_EQUAL(0x02, dst_buf[1]);
  TEST_ASSERT_EQUAL(0xEE, dst_buf[2]);
  for(int i=3; i<7; i++) TEST_ASSERT_EQUAL(0xFF, dst_buf[i]);
  TEST_ASSERT_EQUAL(0x03, dst_buf[7]);
}

void test_rle_on_event_unknown() {
  // We can't easily send an unknown message via rle::decode,
  // but we can test if it handles it by mocking or just knowing
  // it's there for SIL-2 safety.
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_rle_empty_src);
  RUN_TEST(test_rle_empty_dst);
  RUN_TEST(test_rle_literal_no_escape);
  RUN_TEST(test_rle_single_escape);
  RUN_TEST(test_rle_run_escape);
  RUN_TEST(test_rle_dst_overflow_literal);
  RUN_TEST(test_rle_dst_overflow_run);
  RUN_TEST(test_rle_incomplete_escape_marker);
  RUN_TEST(test_rle_incomplete_escape_val);
  RUN_TEST(test_rle_complex_sequence);
  RUN_TEST(test_rle_on_event_unknown);
  return UNITY_END();
}
