#include "rle.h"
#include <etl/algorithm.h>
#include <etl/iterator.h>

namespace rle {

size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
  if (src.empty() || dst.empty()) return 0;

  auto dst_it = dst.begin();

  enum class State { LITERAL, ESC_MARKER, ESC_VAL };
  State state = State::LITERAL;
  uint8_t esc_count = 0;
  bool error = false;

  (void)etl::find_if(src.begin(), src.end(), [&](uint8_t b) {
    if (state == State::LITERAL) {
      if (b == ESCAPE_BYTE) {
        state = State::ESC_MARKER;
      } else {
        if (dst_it == dst.end()) { error = true; return true; }
        *dst_it++ = b;
      }
    } else if (state == State::ESC_MARKER) {
      esc_count = b;
      state = State::ESC_VAL;
    } else if (state == State::ESC_VAL) {
      size_t run_len = (esc_count == SINGLE_ESCAPE_MARKER) ? 1 : static_cast<size_t>(esc_count) + rpc::RPC_RLE_OFFSET;
      if (static_cast<size_t>(etl::distance(dst_it, dst.end())) < run_len) {
        error = true;
        return true;
      }
      etl::fill_n(dst_it, run_len, b);
      dst_it += run_len;
      state = State::LITERAL;
    }
    return false;
  });

  if (error || state != State::LITERAL) return 0;
  return etl::distance(dst.begin(), dst_it);
}

} // namespace rle
