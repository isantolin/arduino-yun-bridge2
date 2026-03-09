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

class SHA256 {
 public:
  static const size_t HASH_SIZE = 32;
  static const size_t BLOCK_SIZE = 64;

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
inline void hkdf_sha256(void* out, size_t outLen, const void* key, size_t keyLen,
                        const void* salt, size_t saltLen, const void* info,
                        size_t infoLen) {
  SHA256 hash;
  etl::array<uint8_t, SHA256::HASH_SIZE> prk;

  // --- Extract phase: PRK = HMAC-Hash(salt, IKM) ---
  const uint8_t* s;
  size_t slen;
  // cppcheck-suppress variableScope ; False positive: Scope must be wide to maintain pointer lifetime post-assignment
  etl::array<uint8_t, SHA256::HASH_SIZE> zero_salt;
  if (salt && saltLen) {
    s = static_cast<const uint8_t*>(salt);
    slen = saltLen;
  } else {
    zero_salt.fill(0);
    s = zero_salt.data();
    slen = SHA256::HASH_SIZE;
  }
  hash.resetHMAC(s, slen);
  hash.update(key, keyLen);
  hash.finalizeHMAC(s, slen, prk.data(), SHA256::HASH_SIZE);

  // --- Expand phase: T(i) = HMAC-Hash(PRK, T(i-1) || info || counter) ---
  etl::array<uint8_t, SHA256::HASH_SIZE> t_block;
  uint8_t* outPtr = static_cast<uint8_t*>(out);
  uint8_t counter = 1;

  while (outLen > 0) {
    hash.resetHMAC(prk.data(), SHA256::HASH_SIZE);
    if (counter > 1) {
      hash.update(t_block.data(), SHA256::HASH_SIZE);
    }
    if (info && infoLen) {
      hash.update(info, infoLen);
    }
    hash.update(&counter, 1);
    hash.finalizeHMAC(prk.data(), SHA256::HASH_SIZE, t_block.data(),
                      SHA256::HASH_SIZE);
    ++counter;

    size_t n = etl::min(outLen, size_t(SHA256::HASH_SIZE));
    etl::copy_n(t_block.data(), n, outPtr);
    outPtr += n;
    outLen -= n;
  }

  // Securely zero sensitive material.
  volatile uint8_t* p = prk.data();
  for (size_t i = 0; i < SHA256::HASH_SIZE; i++) *p++ = 0;
  p = t_block.data();
  for (size_t i = 0; i < SHA256::HASH_SIZE; i++) *p++ = 0;
}

#endif  // MCUBRIDGE_SHA256_H
