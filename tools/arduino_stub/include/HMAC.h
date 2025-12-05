#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>

// Minimal host-side implementation of the Arduino Crypto HMAC helper.
template <typename Hash>
class HMAC {
 public:
  explicit HMAC(Hash& scratch) : scratch_(scratch) {}

  void reset(const uint8_t* key, size_t key_len) {
    uint8_t key_block[Hash::kBlockSize];
    if (key_len > Hash::kBlockSize) {
      uint8_t hashed[Hash::kDigestSize];
      scratch_.reset();
      scratch_.update(key, key_len);
      scratch_.finalize(hashed, Hash::kDigestSize);
      std::memset(key_block, 0, Hash::kBlockSize);
      std::memcpy(key_block, hashed, Hash::kDigestSize);
    } else {
      std::memset(key_block, 0, Hash::kBlockSize);
      if (key != nullptr && key_len > 0) {
        std::memcpy(key_block, key, key_len);
      }
    }

    uint8_t ipad[Hash::kBlockSize];
    uint8_t opad[Hash::kBlockSize];
    for (size_t i = 0; i < Hash::kBlockSize; ++i) {
      ipad[i] = key_block[i] ^ 0x36;
      opad[i] = key_block[i] ^ 0x5C;
    }

    inner_.reset();
    inner_.update(ipad, Hash::kBlockSize);

    outer_.reset();
    outer_.update(opad, Hash::kBlockSize);
  }

  void update(const uint8_t* data, size_t len) {
    if (data == nullptr || len == 0) {
      return;
    }
    inner_.update(data, len);
  }

  void finalize(uint8_t* out, size_t len) {
    uint8_t inner_digest[Hash::kDigestSize];
    inner_.finalize(inner_digest, Hash::kDigestSize);

    Hash outer_work = outer_;
    outer_work.update(inner_digest, Hash::kDigestSize);

    uint8_t final_digest[Hash::kDigestSize];
    outer_work.finalize(final_digest, Hash::kDigestSize);

    const size_t to_copy = std::min(len, static_cast<size_t>(Hash::kDigestSize));
    if (out != nullptr && to_copy > 0) {
      std::memcpy(out, final_digest, to_copy);
    }
  }

 private:
  Hash& scratch_;
  Hash inner_;
  Hash outer_;
};
