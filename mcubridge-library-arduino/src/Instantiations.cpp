#include <stdint.h>
#include "etl_profile.h"
#include <etl/span.h>
#include <etl/delegate.h>
#include <etl/expected.h>
#include "protocol/rpc_frame.h"

// [SIL-2] Explicit Template Instantiations to reduce binary bloat
// This ensures these common types are compiled only once.

namespace etl {
  template class span<uint8_t>;
  template class span<const uint8_t>;
  template class span<char>;
  template class span<const char>;

  // Common delegates used in Bridge
  template class delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  template class delegate<void(const rpc_pb_RpcEnvelope&)>;
}
