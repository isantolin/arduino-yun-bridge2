#include <etl/byte_stream.h>
#include <etl/delegate.h>
#include <etl/expected.h>
#include <etl/span.h>
#include <stdint.h>

#include "etl_profile.h"
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
}  // namespace etl

namespace rpc {

etl::expected<rpc_pb_RpcEnvelope, FrameError> parse_frame(
    etl::span<const uint8_t> buffer) {
  if (buffer.size() < CRC_TRAILER_SIZE + 2U)
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
  const uint32_t crc_calc = checksum::compute(buffer.subspan(0, crc_offset));
  uint32_t crc_received = 0;
  const auto crc_tail = buffer.subspan(crc_offset);
  etl::byte_stream_reader reader(crc_tail.data(), crc_tail.size(),
                                 etl::endian::little);
  if (auto val = reader.read<uint32_t>()) {
    crc_received = *val;
  }
  if (crc_received != crc_calc)
    return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  pb_istream_t stream = pb_istream_from_buffer(buffer.data(), crc_offset);
  if (!pb_decode(&stream, rpc_pb_RpcEnvelope_fields, &env))
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  if (env.version != PROTOCOL_VERSION)
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  return env;
}

}  // namespace rpc
