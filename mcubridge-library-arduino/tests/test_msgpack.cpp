#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "protocol/msgpack_codec.h"
#include "test_support.h"

// Define the global delegates and stubs for HardwareSerial stub
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;

// Unity setup/teardown
void setUp(void) {}
void tearDown(void) {}

void test_msgpack_encode_decode_int() {
  uint8_t buffer[20];
  msgpack::Encoder encoder(buffer, 20);
  
  encoder.write_uint8(42);
  encoder.write_uint16(1000);
  encoder.write_uint32(1000000);
  
  msgpack::Decoder decoder(buffer, encoder.size());
  
  TEST_ASSERT_EQUAL(42, decoder.read_uint8());
  TEST_ASSERT_EQUAL(1000, decoder.read_uint16());
  TEST_ASSERT_EQUAL(1000000, decoder.read_uint32());
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_encode_decode_string() {
  uint8_t buffer[200];
  msgpack::Encoder encoder(buffer, sizeof(buffer));
  
  const char* test_str = "hello";
  encoder.write_str(test_str, strlen(test_str));
  
  char str32[33];
  memset(str32, 'A', 32);
  str32[32] = '\0';
  encoder.write_str(str32, 32);
  
  msgpack::Decoder decoder(buffer, encoder.size());
  
  auto view1 = decoder.read_str_view();
  TEST_ASSERT_EQUAL(5, view1.size());
  TEST_ASSERT_EQUAL_MEMORY("hello", view1.data(), 5);
  
  auto view2 = decoder.read_str_view();
  TEST_ASSERT_EQUAL(32, view2.size());
  TEST_ASSERT_EQUAL_MEMORY(str32, view2.data(), 32);
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_encode_decode_bytes() {
  uint8_t buffer[300];
  msgpack::Encoder encoder(buffer, sizeof(buffer));
  
  uint8_t test_bytes[] = {0xDE, 0xAD, 0xBE, 0xEF};
  encoder.write_bin(etl::span<const uint8_t>(test_bytes));
  
  uint8_t large_bytes[260];
  memset(large_bytes, 0xCC, 260);
  encoder.write_bin(etl::span<const uint8_t>(large_bytes, 260));
  
  msgpack::Decoder decoder(buffer, encoder.size());
  
  auto view1 = decoder.read_bin_view();
  TEST_ASSERT_EQUAL(4, view1.size());
  TEST_ASSERT_EQUAL_HEX8_ARRAY(test_bytes, view1.data(), 4);
  
  auto view2 = decoder.read_bin_view();
  TEST_ASSERT_EQUAL(260, view2.size());
  TEST_ASSERT_EQUAL_HEX8_ARRAY(large_bytes, view2.data(), 260);
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_decode_error_overflow() {
  uint8_t buffer[] = {0x92}; 
  msgpack::Decoder decoder(buffer, 1);
  decoder.read_uint32();
  TEST_ASSERT(!decoder.ok());
}

void test_msgpack_array_fix_and_16() {
  uint8_t buffer[50];
  msgpack::Encoder encoder(buffer, sizeof(buffer));
  encoder.write_array(3);
  encoder.write_array(20); 
  
  msgpack::Decoder decoder(buffer, encoder.size());
  TEST_ASSERT_EQUAL(3, decoder.read_array());
  TEST_ASSERT_EQUAL(20, decoder.read_array());
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_encoder_error_overflow() {
  uint8_t buffer[1];
  msgpack::Encoder encoder(buffer, 1);
  encoder.write_uint32(1000000); 
  TEST_ASSERT(!encoder.ok());
}

void test_msgpack_decoder_error_paths() {
  uint8_t buffer[] = {0xCE}; 
  msgpack::Decoder decoder(buffer, 1);
  TEST_ASSERT_EQUAL(0, decoder.read_uint32());
  TEST_ASSERT(!decoder.ok());
}

void test_msgpack_large_headers() {
  uint8_t buffer[100];
  msgpack::Encoder encoder(buffer, sizeof(buffer));
  encoder.write_array(16);
  
  msgpack::Decoder decoder(buffer, encoder.size());
  TEST_ASSERT_EQUAL(16, decoder.read_array());
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_int_edge_cases() {
    uint8_t buffer[50];
    msgpack::Encoder encoder(buffer, sizeof(buffer));
    encoder.write_uint16(200); 
    encoder.write_uint32(70000); 
    
    msgpack::Decoder decoder(buffer, encoder.size());
    TEST_ASSERT_EQUAL(200, decoder.read_uint16());
    TEST_ASSERT_EQUAL(70000, decoder.read_uint32());
    TEST_ASSERT(decoder.ok());
}

void test_msgpack_large_data_formats() {
    uint8_t buffer[1000];
    msgpack::Encoder encoder(buffer, sizeof(buffer));
    char str256[257];
    memset(str256, 'B', 256);
    str256[256] = '\0';
    encoder.write_str(str256, 256);
    
    msgpack::Decoder decoder(buffer, encoder.size());
    auto view = decoder.read_str_view();
    TEST_ASSERT_EQUAL(256, view.size());
    TEST_ASSERT(decoder.ok());
}

void test_msgpack_error_mismatches() {
    uint8_t buffer[10];
    msgpack::Encoder enc(buffer, 10);
    enc.write_uint8(1);
    
    {
        msgpack::Decoder dec(buffer, 1);
        (void)dec.read_str_view(); 
        TEST_ASSERT(!dec.ok());
    }
    {
        msgpack::Decoder dec(buffer, 1);
        (void)dec.read_bin_view(); 
        TEST_ASSERT(!dec.ok());
    }
}

void test_msgpack_32bit_formats() {
    static uint8_t buffer[70000];
    msgpack::Encoder encoder(buffer, sizeof(buffer));
    static uint8_t large_bin[66000];
    memset(large_bin, 0xDD, 66000);
    encoder.write_bin(etl::span<const uint8_t>(large_bin, 66000));
    
    msgpack::Decoder decoder(buffer, encoder.size());
    auto view = decoder.read_bin_view();
    TEST_ASSERT_EQUAL(66000, view.size());
    TEST_ASSERT(decoder.ok());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_msgpack_encode_decode_int);
  RUN_TEST(test_msgpack_encode_decode_string);
  RUN_TEST(test_msgpack_encode_decode_bytes);
  RUN_TEST(test_msgpack_decode_error_overflow);
  RUN_TEST(test_msgpack_array_fix_and_16);
  RUN_TEST(test_msgpack_encoder_error_overflow);
  RUN_TEST(test_msgpack_decoder_error_paths);
  RUN_TEST(test_msgpack_large_headers);
  RUN_TEST(test_msgpack_int_edge_cases);
  RUN_TEST(test_msgpack_large_data_formats);
  RUN_TEST(test_msgpack_error_mismatches);
  RUN_TEST(test_msgpack_32bit_formats);
  return UNITY_END();
}
