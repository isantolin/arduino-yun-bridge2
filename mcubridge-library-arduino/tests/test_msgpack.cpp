#include <etl/array.h>
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
  etl::array<uint8_t, 20> buffer = {};
  msgpack::Encoder encoder(buffer.data(), buffer.size());
  
  encoder.write_uint8(42);
  encoder.write_uint16(1000);
  encoder.write_uint32(1000000);
  
  msgpack::Decoder decoder(buffer.data(), encoder.size());
  
  TEST_ASSERT_EQUAL(42, decoder.read_uint8());
  TEST_ASSERT_EQUAL(1000, decoder.read_uint16());
  TEST_ASSERT_EQUAL(1000000, decoder.read_uint32());
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_encode_decode_string() {
  etl::array<uint8_t, 200> buffer = {};
  msgpack::Encoder encoder(buffer.data(), buffer.size());
  
  const char* test_str = "hello";
  encoder.write_str(test_str, strlen(test_str));
  
  etl::array<char, 33> str32;
  str32.fill('A');
  str32[32] = '\0';
  encoder.write_str(str32.data(), 32);
  
  msgpack::Decoder decoder(buffer.data(), encoder.size());
  
  auto view1 = decoder.read_str_view();
  TEST_ASSERT_EQUAL(5, view1.size());
  TEST_ASSERT_EQUAL_MEMORY("hello", view1.data(), 5);
  
  auto view2 = decoder.read_str_view();
  TEST_ASSERT_EQUAL(32, view2.size());
  TEST_ASSERT_EQUAL_MEMORY(str32.data(), view2.data(), 32);
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_encode_decode_bytes() {
  etl::array<uint8_t, 300> buffer = {};
  msgpack::Encoder encoder(buffer.data(), buffer.size());
  
  etl::array<uint8_t, 4> test_bytes = {0xDE, 0xAD, 0xBE, 0xEF};
  encoder.write_bin(etl::span<const uint8_t>(test_bytes.data(), 4));
  
  etl::array<uint8_t, 260> large_bytes;
  large_bytes.fill(0xCC);
  encoder.write_bin(etl::span<const uint8_t>(large_bytes.data(), 260));
  
  msgpack::Decoder decoder(buffer.data(), encoder.size());
  
  auto view1 = decoder.read_bin_view();
  TEST_ASSERT_EQUAL(4, view1.size());
  TEST_ASSERT_EQUAL_HEX8_ARRAY(test_bytes.data(), view1.data(), 4);
  
  auto view2 = decoder.read_bin_view();
  TEST_ASSERT_EQUAL(260, view2.size());
  TEST_ASSERT_EQUAL_HEX8_ARRAY(large_bytes.data(), view2.data(), 260);
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_decode_error_overflow() {
  etl::array<uint8_t, 1> buffer = {0x92}; 
  msgpack::Decoder decoder(buffer.data(), 1);
  decoder.read_uint32();
  TEST_ASSERT(!decoder.ok());
}

void test_msgpack_array_fix_and_16() {
  etl::array<uint8_t, 50> buffer = {};
  msgpack::Encoder encoder(buffer.data(), buffer.size());
  encoder.write_array(3);
  encoder.write_array(20); 
  
  msgpack::Decoder decoder(buffer.data(), encoder.size());
  TEST_ASSERT_EQUAL(3, decoder.read_array());
  TEST_ASSERT_EQUAL(20, decoder.read_array());
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_encoder_error_overflow() {
  etl::array<uint8_t, 1> buffer = {};
  msgpack::Encoder encoder(buffer.data(), 1);
  encoder.write_uint32(1000000); 
  TEST_ASSERT(!encoder.ok());
}

void test_msgpack_decoder_error_paths() {
  etl::array<uint8_t, 1> buffer = {0xCE}; 
  msgpack::Decoder decoder(buffer.data(), 1);
  TEST_ASSERT_EQUAL(0, decoder.read_uint32());
  TEST_ASSERT(!decoder.ok());
  
  // get_multi error
  msgpack::Decoder decoder2(buffer.data(), 1);
  decoder2.read_uint16();
  TEST_ASSERT(!decoder2.ok());
}

void test_msgpack_large_headers() {
  etl::array<uint8_t, 100> buffer = {};
  msgpack::Encoder encoder(buffer.data(), buffer.size());
  encoder.write_array(16);
  
  msgpack::Decoder decoder(buffer.data(), encoder.size());
  TEST_ASSERT_EQUAL(16, decoder.read_array());
  TEST_ASSERT(decoder.ok());
}

void test_msgpack_int_edge_cases() {
    etl::array<uint8_t, 50> buffer = {};
    msgpack::Encoder encoder(buffer.data(), buffer.size());
    encoder.write_uint16(200); 
    encoder.write_uint32(70000); 
    
    msgpack::Decoder decoder(buffer.data(), encoder.size());
    TEST_ASSERT_EQUAL(200, decoder.read_uint16());
    TEST_ASSERT_EQUAL(70000, decoder.read_uint32());
    TEST_ASSERT(decoder.ok());
}

void test_msgpack_large_data_formats() {
    etl::array<uint8_t, 1000> buffer = {};
    msgpack::Encoder encoder(buffer.data(), buffer.size());
    etl::array<char, 257> str256;
    str256.fill('B');
    str256[256] = '\0';
    encoder.write_str(str256.data(), 256);
    
    msgpack::Decoder decoder(buffer.data(), encoder.size());
    auto view = decoder.read_str_view();
    TEST_ASSERT_EQUAL(256, view.size());
    TEST_ASSERT(decoder.ok());
}

void test_msgpack_error_mismatches() {
    etl::array<uint8_t, 10> buffer = {};
    msgpack::Encoder enc(buffer.data(), 10);
    enc.write_uint8(1);
    
    {
        msgpack::Decoder dec(buffer.data(), 1);
        (void)dec.read_str_view(); 
        TEST_ASSERT(!dec.ok());
    }
    {
        msgpack::Decoder dec(buffer.data(), 1);
        (void)dec.read_bin_view(); 
        TEST_ASSERT(!dec.ok());
    }
}

void test_msgpack_32bit_formats() {
    static etl::array<uint8_t, 70000> buffer = {};
    msgpack::Encoder encoder(buffer.data(), buffer.size());
    static etl::array<uint8_t, 66000> large_bin;
    large_bin.fill(0xDD);
    encoder.write_bin(etl::span<const uint8_t>(large_bin.data(), 66000));
    
    static etl::array<char, 66000> large_str;
    large_str.fill('S');
    encoder.write_str(large_str.data(), 66000);
    
    msgpack::Decoder decoder(buffer.data(), encoder.size());
    auto view = decoder.read_bin_view();
    TEST_ASSERT_EQUAL(66000, view.size());
    
    auto sview = decoder.read_str_view();
    TEST_ASSERT_EQUAL(66000, sview.size());
    TEST_ASSERT(decoder.ok());
}

void test_msgpack_write_error_paths() {
    etl::array<uint8_t, 1> buffer = {};
    msgpack::Encoder encoder(buffer.data(), 0); // 0 capacity
    encoder.write_uint8(1);
    TEST_ASSERT(!encoder.ok());
    
    msgpack::Encoder encoder2(buffer.data(), 0);
    encoder2.write_str("test", 4);
    TEST_ASSERT(!encoder2.ok());

    msgpack::Encoder encoder3(buffer.data(), 0);
    encoder3.write_bin(etl::span<const uint8_t>(buffer));
    TEST_ASSERT(!encoder3.ok());
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
  RUN_TEST(test_msgpack_write_error_paths);
  return UNITY_END();
}
