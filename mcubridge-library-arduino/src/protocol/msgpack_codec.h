/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 *
 * Minimal MsgPack encoder/decoder for embedded targets.
 * Supports only the subset used by the MCU Bridge protocol:
 *   fixarray, fixint/uint8/uint16/uint32, bin8/bin16, fixstr/str8.
 * Header-only, no heap allocation, ETL byte-stream based.
 */
#ifndef MSGPACK_CODEC_H
#define MSGPACK_CODEC_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <etl/span.h>
#include <etl/algorithm.h>
#include <etl/byte_stream.h>
#include "rpc_protocol.h"

namespace msgpack {

// ─── Encoder ────────────────────────────────────────────────────────────────
class Encoder {
 public:
  Encoder(uint8_t* buf, size_t cap)
      : _writer(buf, buf + cap, etl::endian::big) {}
  explicit Encoder(etl::span<uint8_t> buf)
      : _writer(buf, etl::endian::big) {}

  bool ok() const { return _ok; }
  size_t size() const { return _writer.size_bytes(); }
  etl::span<const uint8_t> result() const {
    auto d = _writer.used_data();
    return {reinterpret_cast<const uint8_t*>(d.data()), d.size()};
  }

  void write_array(uint8_t count) {
    if (count <= rpc::MSGPACK_FIXARRAY_VALUE_MASK) { put(static_cast<uint8_t>(rpc::MSGPACK_FIXARRAY_MASK | count)); }
    else { _ok = false; }
  }

  void write_uint8(uint8_t v) {
    if (v <= rpc::MSGPACK_POSITIVE_FIXINT_MAX) { put(v); }
    else { put(rpc::MSGPACK_UINT8_FMT); put(v); }
  }

  void write_uint16(uint16_t v) {
    if (v <= rpc::MSGPACK_POSITIVE_FIXINT_MAX)  { put(static_cast<uint8_t>(v)); }
    else if (v <= rpc::MSGPACK_UINT8_MAX_VAL)    { put(rpc::MSGPACK_UINT8_FMT); put(static_cast<uint8_t>(v)); }
    else                                          { put(rpc::MSGPACK_UINT16_FMT); put_multi(v); }
  }

  void write_uint32(uint32_t v) {
    if (v <= rpc::MSGPACK_POSITIVE_FIXINT_MAX)  { put(static_cast<uint8_t>(v)); }
    else if (v <= rpc::MSGPACK_UINT8_MAX_VAL)    { put(rpc::MSGPACK_UINT8_FMT); put(static_cast<uint8_t>(v)); }
    else if (v <= rpc::MSGPACK_UINT16_MAX_VAL)   { put(rpc::MSGPACK_UINT16_FMT); put_multi(static_cast<uint16_t>(v)); }
    else                                          { put(rpc::MSGPACK_UINT32_FMT); put_multi(v); }
  }

  void write_bin(etl::span<const uint8_t> data) {
    const size_t len = data.size();
    if (len <= rpc::MSGPACK_UINT8_MAX_VAL) { put(rpc::MSGPACK_BIN8); put(static_cast<uint8_t>(len)); }
    else                                    { put(rpc::MSGPACK_BIN16); put_multi(static_cast<uint16_t>(len)); }
    write_bytes(data.data(), len);
  }

  void write_str(const char* s, size_t len) {
    if (len <= rpc::MSGPACK_FIXSTR_VALUE_MASK)  { put(static_cast<uint8_t>(rpc::MSGPACK_FIXSTR_MASK | len)); }
    else if (len <= rpc::MSGPACK_UINT8_MAX_VAL)  { put(rpc::MSGPACK_STR8); put(static_cast<uint8_t>(len)); }
    else                                          { _ok = false; return; }
    write_bytes(reinterpret_cast<const uint8_t*>(s), len);
  }

 private:
  void put(uint8_t byte) {
    if (!_writer.write(byte)) { _ok = false; }
  }

  template <typename T>
  void put_multi(T value) {
    if (!_writer.write(value)) { _ok = false; }
  }

  void write_bytes(const uint8_t* data, size_t len) {
    if (!_writer.write(etl::span<const uint8_t>(data, len))) { _ok = false; }
  }

  etl::byte_stream_writer _writer;
  bool _ok = true;
};

// ─── Decoder ────────────────────────────────────────────────────────────────
class Decoder {
 public:
  Decoder(const uint8_t* buf, size_t len)
      : _reader(buf, buf + len, etl::endian::big) {}
  explicit Decoder(etl::span<const uint8_t> buf)
      : _reader(buf.data(), buf.data() + buf.size(), etl::endian::big) {}

  bool ok() const { return _ok; }
  size_t remaining() const { return _ok ? _reader.available_bytes() : 0; }

  uint8_t read_array() {
    const uint8_t b = get();
    if ((b & rpc::MSGPACK_FIXARRAY_TYPE_MASK) == rpc::MSGPACK_FIXARRAY_MASK) { return static_cast<uint8_t>(b & rpc::MSGPACK_FIXARRAY_VALUE_MASK); }
    _ok = false;
    return 0;
  }

  uint8_t read_uint8() { return static_cast<uint8_t>(read_uint32()); }

  uint16_t read_uint16() { return static_cast<uint16_t>(read_uint32()); }

  uint32_t read_uint32() {
    const uint8_t b = get();
    if (b <= rpc::MSGPACK_POSITIVE_FIXINT_MAX) { return b; }
    if (b == rpc::MSGPACK_UINT8_FMT)  { return get(); }
    if (b == rpc::MSGPACK_UINT16_FMT) { return get_multi<uint16_t>(); }
    if (b == rpc::MSGPACK_UINT32_FMT) { return get_multi<uint32_t>(); }
    _ok = false;
    return 0;
  }

  /** Read bin field into provided span, shrinking span to actual length. */
  void read_bin(etl::span<uint8_t>& dst) {
    const size_t len = read_bin_length();
    if (!_ok) { dst = {}; return; }
    if (len > dst.size() || _reader.available_bytes() < len) { _ok = false; dst = {}; return; }
    auto view = _reader.read<uint8_t>(len);
    if (!view.has_value()) { _ok = false; dst = {}; return; }
    memcpy(dst.data(), view.value().data(), len);
    dst = dst.first(len);
  }

  /** Read bin field, returning a zero-copy view into the source buffer.
   *  WARNING: The returned span's lifetime is tied to the source buffer.
   *  Consume the data before the underlying frame buffer is overwritten. */
  [[nodiscard]] etl::span<const uint8_t> read_bin_view() {
    const size_t len = read_bin_length();
    if (!_ok || _reader.available_bytes() < len) { _ok = false; return {}; }
    auto view = _reader.read<uint8_t>(len);
    if (!view.has_value()) { _ok = false; return {}; }
    return view.value();
  }

  /** Read str field into a char buffer with null-termination. Returns chars written. */
  size_t read_str(char* dst, size_t dst_size) {
    const size_t len = read_str_length();
    if (!_ok) { return 0; }
    if (dst_size == 0) { skip(len); return 0; }
    const size_t copy_len = etl::min(len, dst_size - 1);
    if (_reader.available_bytes() < len) { _ok = false; return 0; }
    auto view = _reader.read<uint8_t>(len);
    if (!view.has_value()) { _ok = false; return 0; }
    memcpy(dst, view.value().data(), copy_len);
    dst[copy_len] = '\0';
    return copy_len;
  }

 private:
  uint8_t get() {
    auto opt = _reader.read<uint8_t>();
    if (opt.has_value()) { return opt.value(); }
    _ok = false;
    return 0;
  }

  template <typename T>
  T get_multi() {
    auto opt = _reader.read<T>();
    if (opt.has_value()) { return opt.value(); }
    _ok = false;
    return 0;
  }

  size_t read_bin_length() {
    const uint8_t b = get();
    if (b == rpc::MSGPACK_BIN8)  { return get(); }
    if (b == rpc::MSGPACK_BIN16) { return get_multi<uint16_t>(); }
    _ok = false;
    return 0;
  }

  size_t read_str_length() {
    const uint8_t b = get();
    if ((b & rpc::MSGPACK_FIXSTR_TYPE_MASK) == static_cast<uint8_t>(rpc::MSGPACK_FIXSTR_MASK & rpc::MSGPACK_FIXSTR_TYPE_MASK)) { return static_cast<uint8_t>(b & rpc::MSGPACK_FIXSTR_VALUE_MASK); }
    if (b == rpc::MSGPACK_STR8) { return get(); }
    _ok = false;
    return 0;
  }

  void skip(size_t n) {
    if (!_reader.skip<uint8_t>(n)) { _ok = false; }
  }

  etl::byte_stream_reader _reader;
  bool _ok = true;
};

}  // namespace msgpack

#endif  // MSGPACK_CODEC_H
