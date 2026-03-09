/**
 * @file sha256.cpp
 * @brief SHA-256 / HMAC-SHA256 implementation optimized for ATmega32U4.
 */
#include "sha256.h"
#include "security.h"

#include <etl/algorithm.h>
#include <etl/binary.h>

// --- Platform-specific PROGMEM support ---
#ifdef ARDUINO_ARCH_AVR
#include <avr/pgmspace.h>
#else
#ifndef PROGMEM
#define PROGMEM
#endif
#ifndef pgm_read_dword
#define pgm_read_dword(addr) \
  (*reinterpret_cast<const uint32_t*>(addr))  // NOLINT
#endif
#endif

// SHA-256 round constants (FIPS 180-4 §4.2.2).
static const uint32_t K[64] PROGMEM = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

// --- SHA-256 core ---

SHA256::SHA256() { reset(); }

void SHA256::reset() {
  h_[0] = 0x6a09e667;
  h_[1] = 0xbb67ae85;
  h_[2] = 0x3c6ef372;
  h_[3] = 0xa54ff53a;
  h_[4] = 0x510e527f;
  h_[5] = 0x9b05688c;
  h_[6] = 0x1f83d9ab;
  h_[7] = 0x5be0cd19;
  w_.fill(0);
  chunkSize_ = 0;
  length_ = 0;
}

void SHA256::update(const void* data, size_t len) {
  if (len == 0) return;
  const uint8_t* d = static_cast<const uint8_t*>(data);
  length_ += static_cast<uint64_t>(len) << 3;

  while (len > 0) {
    uint8_t room = 64 - chunkSize_;
    if (room > len) room = static_cast<uint8_t>(len);
    etl::copy_n(d, room, reinterpret_cast<uint8_t*>(w_.data()) + chunkSize_);
    chunkSize_ += room;
    len -= room;
    d += room;
    if (chunkSize_ == 64) {
      processChunk();
      chunkSize_ = 0;
    }
  }
}

void SHA256::finalize(void* hash, size_t len) {
  uint8_t* wb = reinterpret_cast<uint8_t*>(w_.data());

  // Pad the last chunk (may need two blocks).
  wb[chunkSize_] = 0x80;
  if (chunkSize_ <= 55) {
    etl::fill_n(wb + chunkSize_ + 1, 55 - chunkSize_, uint8_t(0));
  } else {
    etl::fill_n(wb + chunkSize_ + 1, 63 - chunkSize_, uint8_t(0));
    processChunk();
    etl::fill_n(wb, 56, uint8_t(0));
  }
  w_[14] = etl::reverse_bytes(static_cast<uint32_t>(length_ >> 32));
  w_[15] = etl::reverse_bytes(static_cast<uint32_t>(length_));
  processChunk();

  // Convert hash state to big-endian and copy out.
  etl::array<uint32_t, 8> out;
  etl::transform(h_.begin(), h_.end(), out.begin(),
                 [](uint32_t val) { return etl::reverse_bytes(val); });

  if (len > HASH_SIZE) len = HASH_SIZE;
  etl::copy_n(reinterpret_cast<const uint8_t*>(out.data()), len,
              static_cast<uint8_t*>(hash));
}

void SHA256::processChunk() {
  // Convert first 16 words from big-endian to host byte order.
  etl::transform(w_.begin(), w_.begin() + 16, w_.begin(),
                 [](uint32_t val) { return etl::reverse_bytes(val); });

  uint32_t a = h_[0], b = h_[1], c = h_[2], d = h_[3];
  uint32_t e = h_[4], f = h_[5], g = h_[6], h = h_[7];
  uint32_t t1, t2;
  uint8_t i;

  for (i = 0; i < 16; ++i) {
    t1 = h +
         (etl::rotate_right(e, 6) ^ etl::rotate_right(e, 11) ^
          etl::rotate_right(e, 25)) +
         ((e & f) ^ (~e & g)) + pgm_read_dword(K + i) + w_[i];
    t2 = (etl::rotate_right(a, 2) ^ etl::rotate_right(a, 13) ^
          etl::rotate_right(a, 22)) +
         ((a & b) ^ (a & c) ^ (b & c));
    h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
  }

  for (; i < 64; ++i) {
    t1 = w_[(i - 15) & 0x0F];
    t2 = w_[(i - 2) & 0x0F];
    t1 = w_[i & 0x0F] =
        w_[(i - 16) & 0x0F] + w_[(i - 7) & 0x0F] +
        (etl::rotate_right(t1, 7) ^ etl::rotate_right(t1, 18) ^ (t1 >> 3)) +
        (etl::rotate_right(t2, 17) ^ etl::rotate_right(t2, 19) ^ (t2 >> 10));

    t1 = h +
         (etl::rotate_right(e, 6) ^ etl::rotate_right(e, 11) ^
          etl::rotate_right(e, 25)) +
         ((e & f) ^ (~e & g)) + pgm_read_dword(K + i) + t1;
    t2 = (etl::rotate_right(a, 2) ^ etl::rotate_right(a, 13) ^
          etl::rotate_right(a, 22)) +
         ((a & b) ^ (a & c) ^ (b & c));
    h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
  }

  h_[0] += a; h_[1] += b; h_[2] += c; h_[3] += d;
  h_[4] += e; h_[5] += f; h_[6] += g; h_[7] += h;
}

// --- HMAC implementation ---

void SHA256::formatHMACKey(const void* key, size_t len, uint8_t pad) {
  etl::array<uint8_t, BLOCK_SIZE> k_block;
  k_block.fill(0);

  if (len <= BLOCK_SIZE) {
    etl::copy_n(static_cast<const uint8_t*>(key), len, k_block.begin());
  } else {
    SHA256 h;
    h.update(key, len);
    h.finalize(k_block.data(), HASH_SIZE);
  }

  for (size_t i = 0; i < BLOCK_SIZE; ++i) k_block[i] ^= pad;
  
  reset();
  update(k_block.data(), BLOCK_SIZE);
  rpc::security::secure_zero(k_block.data(), BLOCK_SIZE);
}

void SHA256::resetHMAC(const void* key, size_t keyLen) {
  formatHMACKey(key, keyLen, 0x36);
}

void SHA256::finalizeHMAC(const void* key, size_t keyLen, void* hash,
                          size_t hashLen) {
  etl::array<uint8_t, HASH_SIZE> inner;
  finalize(inner.data(), HASH_SIZE); // Finalize inner hash

  // Outer hash: H(K ^ opad || inner)
  SHA256 outer;
  outer.formatHMACKey(key, keyLen, 0x5C);
  outer.update(inner.data(), HASH_SIZE);
  outer.finalize(hash, hashLen);

  rpc::security::secure_zero(inner.data(), HASH_SIZE);
}
