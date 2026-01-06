/**
 * Tests for RLE compression implementation.
 * 
 * Build: g++ -std=c++17 -I../src -I../../tools/arduino_stub/include \
 *        -o test_rle test_rle.cpp && ./test_rle
 */

#include <cstdio>
#include <cstring>
#include <cstdint>
#include <cassert>
#include "protocol/rle.h"

static int tests_run = 0;
static int tests_passed = 0;

#define TEST(name) \
  do { \
    tests_run++; \
    printf("  %s... ", #name); \
  } while(0)

#define PASS() \
  do { \
    tests_passed++; \
    printf("OK\n"); \
  } while(0)

#define FAIL(msg) \
  do { \
    printf("FAIL: %s\n", msg); \
  } while(0)

// Helper to compare byte arrays
bool bytes_equal(const uint8_t* a, const uint8_t* b, size_t len) {
  for (size_t i = 0; i < len; i++) {
    if (a[i] != b[i]) return false;
  }
  return true;
}

// Helper for roundtrip test
bool roundtrip_ok(const uint8_t* data, size_t len) {
  uint8_t encoded[512];
  uint8_t decoded[512];
  
  size_t enc_len = rle::encode(data, len, encoded, sizeof(encoded));
  if (enc_len == 0 && len > 0) return false;
  
  size_t dec_len = rle::decode(encoded, enc_len, decoded, sizeof(decoded));
  if (dec_len != len) return false;
  
  return bytes_equal(data, decoded, len);
}

void test_empty_input() {
  TEST(empty_input);
  uint8_t src[1] = {0};
  uint8_t dst[16];
  
  size_t enc_len = rle::encode(nullptr, 0, dst, sizeof(dst));
  if (enc_len != 0) { FAIL("encode null should return 0"); return; }
  
  enc_len = rle::encode(src, 0, dst, sizeof(dst));
  if (enc_len != 0) { FAIL("encode empty should return 0"); return; }
  
  PASS();
}

void test_single_byte() {
  TEST(single_byte);
  uint8_t src[1] = {'A'};
  uint8_t dst[16];
  
  size_t enc_len = rle::encode(src, 1, dst, sizeof(dst));
  if (enc_len != 1 || dst[0] != 'A') { FAIL("single byte not literal"); return; }
  
  PASS();
}

void test_no_runs() {
  TEST(no_runs);
  uint8_t src[] = {'A', 'B', 'C', 'D', 'E', 'F'};
  uint8_t dst[16];
  
  size_t enc_len = rle::encode(src, sizeof(src), dst, sizeof(dst));
  if (enc_len != sizeof(src)) { FAIL("length mismatch"); return; }
  if (!bytes_equal(src, dst, sizeof(src))) { FAIL("content mismatch"); return; }
  
  PASS();
}

void test_short_run_not_encoded() {
  TEST(short_run_not_encoded);
  uint8_t src[] = {'A', 'A', 'A'};  // Run of 3, should NOT encode
  uint8_t dst[16];
  
  size_t enc_len = rle::encode(src, sizeof(src), dst, sizeof(dst));
  if (enc_len != 3) { FAIL("short run should be literal"); return; }
  if (!bytes_equal(src, dst, 3)) { FAIL("content mismatch"); return; }
  
  PASS();
}

void test_min_run_encoded() {
  TEST(min_run_encoded);
  uint8_t src[] = {'A', 'A', 'A', 'A'};  // Run of 4
  uint8_t dst[16];
  uint8_t expected[] = {rle::ESCAPE_BYTE, 2, 'A'};  // count-2=2
  
  size_t enc_len = rle::encode(src, sizeof(src), dst, sizeof(dst));
  if (enc_len != 3) { FAIL("encoded length should be 3"); return; }
  if (!bytes_equal(expected, dst, 3)) { FAIL("encoded content mismatch"); return; }
  
  PASS();
}

void test_long_run() {
  TEST(long_run);
  uint8_t src[10];
  memset(src, 'A', 10);  // Run of 10
  uint8_t dst[16];
  uint8_t expected[] = {rle::ESCAPE_BYTE, 8, 'A'};  // count-2=8
  
  size_t enc_len = rle::encode(src, 10, dst, sizeof(dst));
  if (enc_len != 3) { FAIL("encoded length should be 3"); return; }
  if (!bytes_equal(expected, dst, 3)) { FAIL("encoded content mismatch"); return; }
  
  PASS();
}

void test_escape_byte_handling() {
  TEST(escape_byte_handling);
  uint8_t src[] = {0xFF};  // Single escape byte
  uint8_t dst[16];
  // Single 0xFF: ESCAPE, 255 (special marker), 0xFF
  uint8_t expected[] = {rle::ESCAPE_BYTE, 255, rle::ESCAPE_BYTE};
  
  size_t enc_len = rle::encode(src, 1, dst, sizeof(dst));
  if (enc_len != 3) { FAIL("encoded length should be 3"); return; }
  if (!bytes_equal(expected, dst, 3)) { FAIL("encoded content mismatch"); return; }
  
  PASS();
}

void test_mixed_data() {
  TEST(mixed_data);
  // "ABBBBBCD" = A + run(5,B) + C + D
  uint8_t src[] = {'A', 'B', 'B', 'B', 'B', 'B', 'C', 'D'};
  uint8_t dst[16];
  // Expected: 'A' + ESCAPE + 3 + 'B' + 'C' + 'D'
  uint8_t expected[] = {'A', rle::ESCAPE_BYTE, 3, 'B', 'C', 'D'};
  
  size_t enc_len = rle::encode(src, sizeof(src), dst, sizeof(dst));
  if (enc_len != sizeof(expected)) { FAIL("encoded length mismatch"); return; }
  if (!bytes_equal(expected, dst, sizeof(expected))) { FAIL("encoded content mismatch"); return; }
  
  PASS();
}

void test_decode_literal() {
  TEST(decode_literal);
  uint8_t src[] = {'A', 'B', 'C', 'D'};
  uint8_t dst[16];
  
  size_t dec_len = rle::decode(src, sizeof(src), dst, sizeof(dst));
  if (dec_len != sizeof(src)) { FAIL("decoded length mismatch"); return; }
  if (!bytes_equal(src, dst, sizeof(src))) { FAIL("decoded content mismatch"); return; }
  
  PASS();
}

void test_decode_run() {
  TEST(decode_run);
  // ESCAPE, count-2=3, 'A' = 5 A's
  uint8_t src[] = {rle::ESCAPE_BYTE, 3, 'A'};
  uint8_t dst[16];
  uint8_t expected[] = {'A', 'A', 'A', 'A', 'A'};
  
  size_t dec_len = rle::decode(src, sizeof(src), dst, sizeof(dst));
  if (dec_len != 5) { FAIL("decoded length should be 5"); return; }
  if (!bytes_equal(expected, dst, 5)) { FAIL("decoded content mismatch"); return; }
  
  PASS();
}

void test_decode_escaped_escape() {
  TEST(decode_escaped_escape);
  // ESCAPE, 255, 0xFF = single 0xFF (special marker)
  uint8_t src[] = {rle::ESCAPE_BYTE, 255, rle::ESCAPE_BYTE};
  uint8_t dst[16];
  
  size_t dec_len = rle::decode(src, sizeof(src), dst, sizeof(dst));
  if (dec_len != 1) { FAIL("decoded length should be 1"); return; }
  if (dst[0] != 0xFF) { FAIL("decoded content mismatch"); return; }
  
  PASS();
}

void test_decode_malformed() {
  TEST(decode_malformed);
  uint8_t dst[16];
  
  // Truncated: just escape
  uint8_t src1[] = {rle::ESCAPE_BYTE};
  if (rle::decode(src1, 1, dst, sizeof(dst)) != 0) { FAIL("should fail on truncated 1"); return; }
  
  // Truncated: escape + count only
  uint8_t src2[] = {rle::ESCAPE_BYTE, 5};
  if (rle::decode(src2, 2, dst, sizeof(dst)) != 0) { FAIL("should fail on truncated 2"); return; }
  
  PASS();
}

void test_roundtrip_simple() {
  TEST(roundtrip_simple);
  
  uint8_t test1[] = {'H', 'e', 'l', 'l', 'o'};
  if (!roundtrip_ok(test1, sizeof(test1))) { FAIL("Hello failed"); return; }
  
  uint8_t test2[10];
  memset(test2, 'A', 10);
  if (!roundtrip_ok(test2, 10)) { FAIL("AAAAAAAAAA failed"); return; }
  
  uint8_t test3[] = {0, 0, 0, 0, 0};
  if (!roundtrip_ok(test3, 5)) { FAIL("nulls failed"); return; }
  
  uint8_t test4[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  if (!roundtrip_ok(test4, 5)) { FAIL("0xFF run failed"); return; }
  
  PASS();
}

void test_roundtrip_all_bytes() {
  TEST(roundtrip_all_bytes);
  uint8_t data[256];
  for (int i = 0; i < 256; i++) data[i] = i;
  
  if (!roundtrip_ok(data, 256)) { FAIL("all bytes failed"); return; }
  
  PASS();
}

void test_roundtrip_mixed() {
  TEST(roundtrip_mixed);
  // Start + nulls + middle + 0xFF run + end
  uint8_t data[100];
  memcpy(data, "Start", 5);
  memset(data + 5, 0, 50);
  memcpy(data + 55, "Mid", 3);
  memset(data + 58, 0xFF, 30);
  memcpy(data + 88, "End", 3);
  
  if (!roundtrip_ok(data, 91)) { FAIL("mixed failed"); return; }
  
  PASS();
}

void test_should_compress() {
  TEST(should_compress);
  
  // Small data: should NOT compress
  uint8_t small[] = {'A', 'A', 'A', 'A'};
  if (rle::should_compress(small, 4)) { FAIL("small should not compress"); return; }
  
  // Large uniform data: should compress
  uint8_t uniform[50];
  memset(uniform, 'A', 50);
  if (!rle::should_compress(uniform, 50)) { FAIL("uniform should compress"); return; }
  
  PASS();
}

void test_buffer_overflow_protection() {
  TEST(buffer_overflow_protection);
  uint8_t src[10];
  memset(src, 'A', 10);
  uint8_t dst[2];  // Too small
  
  // Should return 0 (failure) instead of overflowing
  size_t enc_len = rle::encode(src, 10, dst, sizeof(dst));
  if (enc_len != 0) { FAIL("should fail on small buffer"); return; }
  
  PASS();
}

void test_max_run_split() {
  TEST(max_run_split);
  // Run of 300 bytes should be split at MAX_RUN_LENGTH (257)
  uint8_t data[300];
  memset(data, 'X', 300);
  
  if (!roundtrip_ok(data, 300)) { FAIL("long run split failed"); return; }
  
  PASS();
}

int main() {
  printf("=== RLE Compression Tests ===\n\n");
  
  printf("Encode tests:\n");
  test_empty_input();
  test_single_byte();
  test_no_runs();
  test_short_run_not_encoded();
  test_min_run_encoded();
  test_long_run();
  test_escape_byte_handling();
  test_mixed_data();
  
  printf("\nDecode tests:\n");
  test_decode_literal();
  test_decode_run();
  test_decode_escaped_escape();
  test_decode_malformed();
  
  printf("\nRoundtrip tests:\n");
  test_roundtrip_simple();
  test_roundtrip_all_bytes();
  test_roundtrip_mixed();
  test_max_run_split();
  
  printf("\nHeuristic tests:\n");
  test_should_compress();
  
  printf("\nSafety tests:\n");
  test_buffer_overflow_protection();
  
  printf("\n=== Results: %d/%d tests passed ===\n", tests_passed, tests_run);
  
  return (tests_passed == tests_run) ? 0 : 1;
}
