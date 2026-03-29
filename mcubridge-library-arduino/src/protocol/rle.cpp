#include "rle.h"
#include <etl/algorithm.h>
#include <etl/iterator.h>

namespace rle {

size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
  if (src.empty() || dst.empty()) return 0;

  auto src_it = src.begin();
  auto dst_it = dst.begin();

  while (src_it != src.end()) {
    // Bulk copy literals until the next escape byte
    auto next_escape = etl::find(src_it, src.end(), ESCAPE_BYTE);
    size_t literal_len = etl::distance(src_it, next_escape);

    if (static_cast<size_t>(etl::distance(dst_it, dst.end())) < literal_len) {
      return 0;
    }
    dst_it = etl::copy(src_it, next_escape, dst_it);
    src_it = next_escape;

    if (src_it != src.end()) {
      // Process Escape block
      src_it++;  // Skip ESCAPE_BYTE
      if (etl::distance(src_it, src.end()) < static_cast<int>(rpc::RPC_RLE_EXPANSION_FACTOR - 1)) return 0;

      uint8_t count_m2 = *src_it++;
      uint8_t val = *src_it++;
      size_t run_len = (count_m2 == SINGLE_ESCAPE_MARKER) ? 1 : static_cast<size_t>(count_m2) + rpc::RPC_RLE_OFFSET;

      if (static_cast<size_t>(etl::distance(dst_it, dst.end())) < run_len) {
        return 0;
      }
      etl::fill_n(dst_it, run_len, val);
      dst_it += run_len;
    }
  }
  return etl::distance(dst.begin(), dst_it);
}

} // namespace rle
