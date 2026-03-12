/**
 * @file sha256.h
 * @brief Minimal SHA-256, HMAC-SHA256, and HKDF-SHA256 for ATmega32U4.
 *
 * Drop-in replacement for the external Crypto library. Eliminates virtual
 * dispatch, base-class vtables, and unused cipher code to save ~2 KB flash
 * on AVR.
 *
 * Conforms to:
 *  - FIPS 180-4 (SHA-256)
 *  - RFC 2104  (HMAC)
 *  - RFC 5869  (HKDF)
 *
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin and contributors.
 */
#ifndef MCUBRIDGE_SHA256_H
#define MCUBRIDGE_SHA256_H

#include <stddef.h>
#include <stdint.h>

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>

class SHA256 {
 public:
  static constexpr size_t HASH_SIZE = 32;
  static constexpr size_t BLOCK_SIZE = 64;

  SHA256();

  void reset();
  void update(const void* data, size_t len);
  void finalize(void* hash, size_t len);

  void resetHMAC(const void* key, size_t keyLen);
  void finalizeHMAC(const void* key, size_t keyLen, void* hash, size_t hashLen);

 private:
  void processChunk();
  void formatHMACKey(const void* key, size_t len, uint8_t pad);

  etl::array<uint32_t, 8> h_;
  etl::array<uint32_t, 16> w_;
  uint64_t length_;
  uint8_t chunkSize_;
};

/**
 * @brief HKDF (RFC 5869) — extract-then-expand using SHA-256.
 */
inline void hkdf_sha256(etl::span<uint8_t> out, etl::span<const uint8_t> key,
                        etl::span<const uint8_t> salt,
                        etl::span<const uint8_t> info) {
  SHA256 hash;
  etl::array<uint8_t, SHA256::HASH_SIZE> prk;

  // --- Extract phase: PRK = HMAC-Hash(salt, IKM) ---
  if (!salt.empty()) {
    hash.resetHMAC(salt.data(), salt.size());
  } else {
    etl::array<uint8_t, SHA256::HASH_SIZE> zero_salt;
    zero_salt.fill(0);
    hash.resetHMAC(zero_salt.data(), zero_salt.size());
  }
  hash.update(key.data(), key.size());
  hash.finalizeHMAC(nullptr, 0, prk.data(), SHA256::HASH_SIZE);

  // --- Expand phase: T(i) = HMAC-Hash(PRK, T(i-1) || info || counter) ---
  etl::array<uint8_t, SHA256::HASH_SIZE> t_block;
  uint8_t counter = 1;
  size_t offset = 0;

  while (offset < out.size()) {
    hash.resetHMAC(prk.data(), SHA256::HASH_SIZE);
    if (counter > 1) {
      hash.update(t_block.data(), SHA256::HASH_SIZE);
    }
    if (!info.empty()) {
      hash.update(info.data(), info.size());
    }
    hash.update(&counter, 1);
    hash.finalizeHMAC(prk.data(), SHA256::HASH_SIZE, t_block.data(),
                      SHA256::HASH_SIZE);
    ++counter;

    size_t n = etl::min(out.size() - offset, size_t(SHA256::HASH_SIZE));
    etl::copy_n(t_block.data(), n, out.begin() + offset);
    offset += n;
  }

  // [MIL-SPEC] Securely zero sensitive material using volatile pointer loop.
  // We use a local implementation to avoid circular dependencies with security.h.
  volatile uint8_t* p;
  p = static_cast<volatile uint8_t*>(prk.data());
  for (size_t i = 0; i < SHA256::HASH_SIZE; i++) p[i] = 0;
  p = static_cast<volatile uint8_t*>(t_block.data());
  for (size_t i = 0; i < SHA256::HASH_SIZE; i++) p[i] = 0;
}

#endif  // MCUBRIDGE_SHA256_H
