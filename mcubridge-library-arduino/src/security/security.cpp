/**
 * @file security.cpp
 * @brief Implementation of security primitives with wolfSSL backend.
 */
 #include "security.h"

 #include <Arduino.h>
 #include "hal/progmem_compat.h"
 #include "user_settings.h"
 #include <wolfssl/wolfcrypt/settings.h>
 #include <wolfssl/wolfcrypt/error-crypt.h>

namespace rpc {
namespace security {

// --- McuBridgeSha256 Implementation ---

McuBridgeSha256::McuBridgeSha256() : is_hmac_active_(false) {
  reset();
}

void McuBridgeSha256::reset() {
  wc_InitSha256(&sha_);
  is_hmac_active_ = false;
}

void McuBridgeSha256::update(const void* data, size_t len) {
  if (is_hmac_active_) {
    wc_HmacUpdate(&hmac_, static_cast<const byte*>(data), static_cast<word32>(len));
  } else {
    wc_Sha256Update(&sha_, static_cast<const byte*>(data), static_cast<word32>(len));
  }
}

void McuBridgeSha256::finalize(void* hash, size_t len) {
  (void)len;
  wc_Sha256Final(&sha_, static_cast<byte*>(hash));
}

void McuBridgeSha256::resetHMAC(const void* key, size_t keyLen) {
  wc_HmacSetKey(&hmac_, WC_SHA256, static_cast<const byte*>(key), static_cast<word32>(keyLen));
  is_hmac_active_ = true;
}

void McuBridgeSha256::finalizeHMAC(const void* key, size_t keyLen, void* hash, size_t hashLen) {
  (void)key; (void)keyLen; (void)hashLen;
  if (is_hmac_active_) {
    wc_HmacFinal(&hmac_, static_cast<byte*>(hash));
  }
  is_hmac_active_ = false;
}

// --- HKDF Implementation ---

void hkdf_sha256(etl::span<uint8_t> out, etl::span<const uint8_t> key,
                 etl::span<const uint8_t> salt,
                 etl::span<const uint8_t> info) {
  
  wc_HKDF(WC_SHA256,
          key.data(), static_cast<word32>(key.size()),
          salt.data(), static_cast<word32>(salt.size()),
          info.data(), static_cast<word32>(info.size()),
          out.data(), static_cast<word32>(out.size()));
}

// --- Self-Tests Implementation ---

constexpr size_t kSha256DigestSize = 32;
constexpr size_t kKatBufferSize = kSha256DigestSize * 2;

static constexpr uint8_t kat_sha256_msg[] PROGMEM = {'a', 'b', 'c'};
static constexpr uint8_t kat_sha256_expected[] PROGMEM = {
    0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
    0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
    0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD};

static constexpr uint8_t kat_hmac_key[] PROGMEM = {'k', 'e', 'y'};
static constexpr uint8_t kat_hmac_data[] PROGMEM = {
    'T', 'h', 'e', ' ', 'q', 'u', 'i', 'c', 'k', ' ', 'b', 'r', 'o', 'w', 'n',
    ' ', 'f', 'o', 'x', ' ', 'j', 'u', 'm', 'p', 's', ' ', 'o', 'v', 'e', 'r',
    ' ', 't', 'h', 'e', ' ', 'l', 'a', 'z', 'y', ' ', 'd', 'o', 'g'};
static constexpr uint8_t kat_hmac_expected[] PROGMEM = {
    0xF7, 0xBC, 0x83, 0xF4, 0x30, 0x53, 0x84, 0x24, 0xB1, 0x32, 0x98,
    0xE6, 0xAA, 0x6F, 0xB1, 0x43, 0xEF, 0x4D, 0x59, 0xA1, 0x49, 0x46,
    0x17, 0x59, 0x97, 0x47, 0x9D, 0xBC, 0x2D, 0x1A, 0x3C, 0xD8};

bool run_cryptographic_self_tests() {
  McuBridgeSha256 sha256;
  etl::array<uint8_t, kSha256DigestSize> actual;
  etl::array<uint8_t, kKatBufferSize> buffer;

  // 1. SHA256 KAT
  sha256.reset();
  size_t msg_len = sizeof(kat_sha256_msg);
  etl::copy_n(kat_sha256_msg, msg_len, buffer.begin());
  sha256.update(buffer.data(), msg_len);
  sha256.finalize(actual.data(), kSha256DigestSize);

  if (memcmp_P(actual.data(), kat_sha256_expected, kSha256DigestSize) != 0)
    return false;

  // 2. HMAC-SHA256 KAT
  etl::array<uint8_t, 32> key_buf;
  size_t key_len = sizeof(kat_hmac_key);
  etl::copy_n(kat_hmac_key, key_len, key_buf.begin());

  sha256.resetHMAC(key_buf.data(), key_len);

  size_t data_len = sizeof(kat_hmac_data);
  etl::copy_n(kat_hmac_data, data_len, buffer.begin());
  sha256.update(buffer.data(), data_len);
  sha256.finalizeHMAC(key_buf.data(), key_len, actual.data(), kSha256DigestSize);

  if (memcmp_P(actual.data(), kat_hmac_expected, kSha256DigestSize) != 0)
    return false;

  return true;
}

}  // namespace security
}  // namespace rpc
