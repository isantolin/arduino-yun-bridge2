#ifndef PB_COPY_H
#define PB_COPY_H

#include <etl/algorithm.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include "nanopb/pb.h"
#include "hal/hal.h"

namespace rpc {
namespace util {

/**
 * @brief Copy a string_view into a nanopb fixed char array.
 *
 * Copies at most (dst_size - 1) characters, leaving room for the null
 * terminator already present in the zero-initialised nanopb struct.
 */
inline void pb_copy_string(etl::string_view src, char* dst, size_t dst_size) {
  if (dst_size == 0) return;
  const size_t to_copy = etl::min(src.length(), dst_size - 1);
  etl::copy_n(src.data(), to_copy, dst);
  dst[to_copy] = rpc::RPC_NULL_TERMINATOR;
}

/**
 * @brief Copy a byte span into a nanopb bytes field (has .bytes + .size).
 *
 * Copies at most sizeof(field.bytes) bytes and sets field.size accordingly.
 */
template <typename PbBytesField>
inline void pb_copy_bytes(etl::span<const uint8_t> src, PbBytesField& field) {
  field.size = static_cast<pb_size_t>(etl::min(src.size(), sizeof(field.bytes)));
  etl::copy_n(src.data(), field.size, field.bytes);
}

/**
 * @brief Setup a nanopb field to use a simple byte-span encoding callback.
 *
 * This enables streaming without copying data into the message structure.
 */
inline bool pb_encode_span_callback(pb_ostream_t* stream, const pb_field_t* field,
                                   void* const* arg) {
  if (arg == nullptr || *arg == nullptr) return true;
  const etl::span<const uint8_t>* span = static_cast<const etl::span<const uint8_t>*>(*arg);
  if (!pb_encode_tag_for_field(stream, field)) return false;
  return pb_encode_string(stream, span->data(), span->size());
}

template <typename PbCallbackField>
inline void pb_setup_encode_span(PbCallbackField& field, const etl::span<const uint8_t>& src) {
  field.funcs.encode = &pb_encode_span_callback;
  field.arg = const_cast<etl::span<const uint8_t>*>(&src);
}

/**
 * @brief Setup a nanopb field to use a simple byte-span decoding callback.
 *
 * The callback will copy incoming bytes into the provided span, up to its capacity.
 * The arg must be a pointer to an etl::span<uint8_t>.
 */
inline bool pb_decode_span_callback(pb_istream_t* stream, const pb_field_t* /* field */,
                                   void** arg) {
  if (arg == nullptr || *arg == nullptr) return true;
  etl::span<uint8_t>* span = static_cast<etl::span<uint8_t>*>(*arg);
  
  size_t to_read = etl::min(static_cast<size_t>(stream->bytes_left), span->size());
  if (!pb_read(stream, span->data(), to_read)) return false;
  
  // Update the span to reflect what was actually read (shrinking it)
  *span = etl::span<uint8_t>(span->data(), to_read);
  return true;
}

template <typename PbCallbackField>
inline void pb_setup_decode_span(PbCallbackField& field, etl::span<uint8_t>& dst) {
  field.funcs.decode = &pb_decode_span_callback;
  field.arg = &dst;
}

/**
 * @brief Join parts (command + args) into a single null-terminated string.
 */
inline void pb_copy_join(etl::string_view base, etl::span<const etl::string_view> parts, char* dst, size_t dst_size) {
  if (dst_size == 0) return;

  char* current = dst;
  char* const end = dst + dst_size - 1; // Reserve room for null terminator

  // Copy base string
  const size_t base_len = etl::min(base.length(), static_cast<size_t>(end - current));
  current = etl::copy_n(base.data(), base_len, current);

  // Join parts with space separator
  for (const auto& part : parts) {
    if (current >= end) break;
    
    // Add separator if there is room for it and at least one character of the part
    *current++ = ' ';
    
    const size_t part_len = etl::min(part.length(), static_cast<size_t>(end - current));
    current = etl::copy_n(part.data(), part_len, current);
  }

  *current = rpc::RPC_NULL_TERMINATOR;
}

} // namespace util
} // namespace rpc

#endif
