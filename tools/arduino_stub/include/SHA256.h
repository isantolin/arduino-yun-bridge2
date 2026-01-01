#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

class SHA256 {
 public:
  static constexpr size_t kDigestSize = 32;
  static constexpr size_t kBlockSize = 64;
  static constexpr uint8_t kByteMask = 0xFFu;
  static constexpr uint8_t kHmacIpadXor = 0x36u;
  static constexpr uint8_t kHmacOpadXor = 0x5Cu;

  SHA256() { reset(); }

  void reset() {
    state_[0] = 0x6A09E667u;
    state_[1] = 0xBB67AE85u;
    state_[2] = 0x3C6EF372u;
    state_[3] = 0xA54FF53Au;
    state_[4] = 0x510E527Fu;
    state_[5] = 0x9B05688Cu;
    state_[6] = 0x1F83D9ABu;
    state_[7] = 0x5BE0CD19u;
    bit_length_ = 0;
    buffer_len_ = 0;
  }

  void update(const uint8_t* data, size_t len) {
    if (!data || len == 0) {
      return;
    }
    while (len > 0) {
      size_t to_copy = kBlockSize - buffer_len_;
      if (to_copy > len) {
        to_copy = len;
      }
      ::memcpy(buffer_ + buffer_len_, data, to_copy);
      buffer_len_ += to_copy;
      data += to_copy;
      len -= to_copy;
      if (buffer_len_ == kBlockSize) {
        transform(buffer_);
        bit_length_ += static_cast<uint64_t>(kBlockSize) * 8u;
        buffer_len_ = 0;
      }
    }
  }

  void finalize(uint8_t* digest, size_t len) {
    bit_length_ += static_cast<uint64_t>(buffer_len_) * 8u;

    buffer_[buffer_len_++] = 0x80u;
    if (buffer_len_ > 56) {
      while (buffer_len_ < kBlockSize) {
        buffer_[buffer_len_++] = 0;
      }
      transform(buffer_);
      buffer_len_ = 0;
    }
    while (buffer_len_ < 56) {
      buffer_[buffer_len_++] = 0;
    }

    for (size_t i = 0; i < 8; ++i) {
      buffer_[63 - i] = static_cast<uint8_t>((bit_length_ >> (i * 8)) & kByteMask);
    }
    transform(buffer_);

    uint8_t full_digest[kDigestSize];
    for (size_t i = 0; i < 8; ++i) {
      full_digest[i * 4 + 0] = static_cast<uint8_t>((state_[i] >> 24) & 0xFFu);
      full_digest[i * 4 + 1] = static_cast<uint8_t>((state_[i] >> 16) & 0xFFu);
      full_digest[i * 4 + 2] = static_cast<uint8_t>((state_[i] >> 8) & 0xFFu);
      full_digest[i * 4 + 3] = static_cast<uint8_t>(state_[i] & kByteMask);
    }

    if (digest && len > 0) {
      if (len > kDigestSize) {
        len = kDigestSize;
      }
      ::memcpy(digest, full_digest, len);
    }
  }

  void resetHMAC(const uint8_t* key, size_t key_len) {
    uint8_t key_block[kBlockSize] = {};
    if (key && key_len > 0) {
      if (key_len > kBlockSize) {
        SHA256 tmp;
        tmp.update(key, key_len);
        tmp.finalize(key_block, kDigestSize);
      } else {
        ::memcpy(key_block, key, key_len);
      }
    }

    for (size_t i = 0; i < kBlockSize; ++i) {
      ipad_[i] = static_cast<uint8_t>(key_block[i] ^ kHmacIpadXor);
      opad_[i] = static_cast<uint8_t>(key_block[i] ^ kHmacOpadXor);
    }

    reset();
    update(ipad_, kBlockSize);
    hmac_active_ = true;
  }

  void finalizeHMAC(
      const uint8_t* /*key*/, size_t /*key_len*/, uint8_t* digest,
      size_t len) {
    uint8_t inner_digest[kDigestSize];
    finalize(inner_digest, kDigestSize);

    SHA256 outer;
    outer.update(opad_, kBlockSize);
    outer.update(inner_digest, kDigestSize);
    outer.finalize(digest, len);
    hmac_active_ = false;
  }

 private:
  static constexpr uint32_t rotr(uint32_t value, uint32_t bits) {
    return (value >> bits) | (value << (32 - bits));
  }

  static constexpr uint32_t choose(uint32_t e, uint32_t f, uint32_t g) {
    return (e & f) ^ (~e & g);
  }

  static constexpr uint32_t majority(uint32_t a, uint32_t b, uint32_t c) {
    return (a & b) ^ (a & c) ^ (b & c);
  }

  void transform(const uint8_t* block) {
    static const uint32_t k[64] = {
        0x428A2F98u, 0x71374491u, 0xB5C0FBCFu, 0xE9B5DBA5u,
        0x3956C25Bu, 0x59F111F1u, 0x923F82A4u, 0xAB1C5ED5u,
        0xD807AA98u, 0x12835B01u, 0x243185BEu, 0x550C7DC3u,
        0x72BE5D74u, 0x80DEB1FEu, 0x9BDC06A7u, 0xC19BF174u,
        0xE49B69C1u, 0xEFBE4786u, 0x0FC19DC6u, 0x240CA1CCu,
        0x2DE92C6Fu, 0x4A7484AAu, 0x5CB0A9DCu, 0x76F988DAu,
        0x983E5152u, 0xA831C66Du, 0xB00327C8u, 0xBF597FC7u,
        0xC6E00BF3u, 0xD5A79147u, 0x06CA6351u, 0x14292967u,
        0x27B70A85u, 0x2E1B2138u, 0x4D2C6DFCu, 0x53380D13u,
        0x650A7354u, 0x766A0ABBu, 0x81C2C92Eu, 0x92722C85u,
        0xA2BFE8A1u, 0xA81A664Bu, 0xC24B8B70u, 0xC76C51A3u,
        0xD192E819u, 0xD6990624u, 0xF40E3585u, 0x106AA070u,
        0x19A4C116u, 0x1E376C08u, 0x2748774Cu, 0x34B0BCB5u,
        0x391C0CB3u, 0x4ED8AA4Au, 0x5B9CCA4Fu, 0x682E6FF3u,
        0x748F82EEu, 0x78A5636Fu, 0x84C87814u, 0x8CC70208u,
        0x90BEFFFAu, 0xA4506CEBu, 0xBEF9A3F7u, 0xC67178F2u};

    uint32_t w[64];
    for (size_t i = 0; i < 16; ++i) {
      w[i] = (static_cast<uint32_t>(block[i * 4]) << 24) |
             (static_cast<uint32_t>(block[i * 4 + 1]) << 16) |
             (static_cast<uint32_t>(block[i * 4 + 2]) << 8) |
             static_cast<uint32_t>(block[i * 4 + 3]);
    }
    for (size_t i = 16; i < 64; ++i) {
      uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
      uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
      w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }

    uint32_t a = state_[0];
    uint32_t b = state_[1];
    uint32_t c = state_[2];
    uint32_t d = state_[3];
    uint32_t e = state_[4];
    uint32_t f = state_[5];
    uint32_t g = state_[6];
    uint32_t h = state_[7];

    for (size_t i = 0; i < 64; ++i) {
      uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      uint32_t ch = choose(e, f, g);
      uint32_t temp1 = h + S1 + ch + k[i] + w[i];
      uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      uint32_t maj = majority(a, b, c);
      uint32_t temp2 = S0 + maj;

      h = g;
      g = f;
      f = e;
      e = d + temp1;
      d = c;
      c = b;
      b = a;
      a = temp1 + temp2;
    }

    state_[0] += a;
    state_[1] += b;
    state_[2] += c;
    state_[3] += d;
    state_[4] += e;
    state_[5] += f;
    state_[6] += g;
    state_[7] += h;
  }

  uint32_t state_[8];
  uint64_t bit_length_ = 0;
  uint8_t buffer_[kBlockSize];
  size_t buffer_len_ = 0;
  uint8_t ipad_[kBlockSize] = {};
  uint8_t opad_[kBlockSize] = {};
  bool hmac_active_ = false;
};
