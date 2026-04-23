/**
 * @file security.cpp
 * @brief Security primitives implementation.
 */
#include "security.h"

#include "../hal/ArchTraits.h"

namespace rpc {
namespace security {

McuBridgeSha256::McuBridgeSha256() : is_hmac_active_(false) {
  wc_InitSha256(&sha_);
}

void McuBridgeSha256::reset() {
  wc_InitSha256(&sha_);
  is_hmac_active_ = false;
}

void McuBridgeSha256::update(etl::span<const uint8_t> data) {
  if (is_hmac_active_) {
    wc_HmacUpdate(&hmac_, data.data(), static_cast<word32>(data.size()));
  } else {
    wc_Sha256Update(&sha_, data.data(), static_cast<word32>(data.size()));
  }
}

void McuBridgeSha256::_finalize_impl(uint8_t* hash, size_t len) {
  (void)len;
  wc_Sha256Final(&sha_, hash);
}

void McuBridgeSha256::resetHMAC(etl::span<const uint8_t> key) {
  wc_HmacSetKey(&hmac_, WC_SHA256, key.data(), static_cast<word32>(key.size()));
  is_hmac_active_ = true;
}

void McuBridgeSha256::_finalize_hmac_impl(uint8_t* hash, size_t len) {
  (void)len;
  wc_HmacFinal(&hmac_, hash);
  is_hmac_active_ = false;
}

// --- HKDF Implementation ---

void hkdf_sha256(etl::span<uint8_t> out, etl::span<const uint8_t> key,
                 etl::span<const uint8_t> salt, etl::span<const uint8_t> info) {
  wc_HKDF(WC_SHA256, key.data(), static_cast<word32>(key.size()), salt.data(),
          static_cast<word32>(salt.size()), info.data(),
          static_cast<word32>(info.size()), out.data(),
          static_cast<word32>(out.size()));
}

// --- Self-Tests Implementation ---

static constexpr etl::array<uint8_t, 3> kat_sha256_msg BRIDGE_PROGMEM = {
    {'a', 'b', 'c'}};
static constexpr etl::array<uint8_t, 32> kat_sha256_expected BRIDGE_PROGMEM = {
    {0xBA, 0x78, 0x16, 0xBF, 0x8F, 0x01, 0xCF, 0xEA, 0x41, 0x41, 0x40,
     0xDE, 0x5D, 0xAE, 0x22, 0x23, 0xB0, 0x03, 0x61, 0xA3, 0x96, 0x17,
     0x7A, 0x9C, 0xB4, 0x10, 0xFF, 0x61, 0xF2, 0x00, 0x15, 0xAD}};

static constexpr etl::array<uint8_t, 3> kat_hmac_key BRIDGE_PROGMEM = {
    {'k', 'e', 'y'}};
static constexpr etl::array<uint8_t, 43> kat_hmac_data BRIDGE_PROGMEM = {
    {'T', 'h', 'e', ' ', 'q', 'u', 'i', 'c', 'k', ' ', 'b', 'r', 'o', 'w', 'n',
     ' ', 'f', 'o', 'x', ' ', 'j', 'u', 'm', 'p', 's', ' ', 'o', 'v', 'e', 'r',
     ' ', 't', 'h', 'e', ' ', 'l', 'a', 'z', 'y', ' ', 'd', 'o', 'g'}};
static constexpr etl::array<uint8_t, 32> kat_hmac_expected BRIDGE_PROGMEM = {
    {0xF7, 0xBC, 0x83, 0xF4, 0x30, 0x53, 0x84, 0x24, 0xB1, 0x32, 0x98,
     0xE6, 0xAA, 0x6F, 0xB1, 0x43, 0xEF, 0x4D, 0x59, 0xA1, 0x49, 0x46,
     0x17, 0x59, 0x97, 0x47, 0x9D, 0xBC, 0x2D, 0x1A, 0x3C, 0xD8}};

bool run_cryptographic_self_tests() {
  using bridge::hal::CurrentArchTraits;
  McuBridgeSha256 sha256;
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> actual;
  etl::array<uint8_t, rpc::RPC_SHA256_KAT_BUFFER_SIZE> buffer;

  // 1. SHA256 KAT
  sha256.reset();
  size_t msg_len = kat_sha256_msg.size();
  CurrentArchTraits::memcpy_to_ram(buffer.data(), kat_sha256_msg.data(),
                                   msg_len);
  sha256.update(etl::span<const uint8_t>(buffer.data(), msg_len));
  sha256.finalize(actual);

  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> expected_buf;
  CurrentArchTraits::memcpy_to_ram(expected_buf.data(),
                                   kat_sha256_expected.data(),
                                   rpc::RPC_SHA256_DIGEST_SIZE);
  if (!etl::equal(actual.begin(), actual.end(), expected_buf.begin()))
    return false;

  // 2. HMAC-SHA256 KAT
  etl::array<uint8_t, rpc::RPC_SHA256_DIGEST_SIZE> key_buf;
  size_t key_len = kat_hmac_key.size();
  CurrentArchTraits::memcpy_to_ram(key_buf.data(), kat_hmac_key.data(), key_len);

  sha256.resetHMAC(etl::span<const uint8_t>(key_buf.data(), key_len));

  size_t data_len = kat_hmac_data.size();
  CurrentArchTraits::memcpy_to_ram(buffer.data(), kat_hmac_data.data(),
                                   data_len);
  sha256.update(etl::span<const uint8_t>(buffer.data(), data_len));
  sha256.finalizeHMAC(actual);

  CurrentArchTraits::memcpy_to_ram(expected_buf.data(),
                                   kat_hmac_expected.data(),
                                   rpc::RPC_SHA256_DIGEST_SIZE);
  if (!etl::equal(actual.begin(), actual.end(), expected_buf.begin()))
    return false;

  return true;
}

}  // namespace security
}  // namespace rpc
