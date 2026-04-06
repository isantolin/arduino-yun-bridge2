/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 *
 * Minimal MsgPack encoder/decoder for embedded targets.
 * Supports only the subset used by the MCU Bridge protocol:
 *   fixarray, fixint/uint8/uint16/uint32, bin8/bin16, fixstr/str8.
 * Header-only, no heap allocation, ETL-based spans.
 */
#ifndef MSGPACK_CODEC_H
#define MSGPACK_CODEC_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <etl/span.h>
#include <etl/algorithm.h>

namespace msgpack {

// ─── Format constants ───────────────────────────────────────────────────────
constexpr uint8_t FIXARRAY_MASK = 0x90;   // 0x90..0x9f  (0-15 elements)
constexpr uint8_t FIXSTR_MASK   = 0xa0;   // 0xa0..0xbf  (0-31 bytes)
constexpr uint8_t BIN8          = 0xc4;
constexpr uint8_t BIN16         = 0xc5;
constexpr uint8_t UINT8_FMT    = 0xcc;
constexpr uint8_t UINT16_FMT   = 0xcd;
constexpr uint8_t UINT32_FMT   = 0xce;
constexpr uint8_t STR8          = 0xd9;

// ─── Encoder ────────────────────────────────────────────────────────────────
class Encoder {
 public:
  Encoder(uint8_t* buf, size_t cap) : _buf(buf), _cap(cap) {}
  explicit Encoder(etl::span<uint8_t> buf) : _buf(buf.data()), _cap(buf.size()) {}

  bool ok() const { return _ok; }
  size_t size() const { return _pos; }
  etl::span<const uint8_t> result() const { return {_buf, _pos}; }

  void write_array(uint8_t count) {
    if (count <= 15) { put(static_cast<uint8_t>(FIXARRAY_MASK | count)); }
    else { _ok = false; }
  }

  void write_uint8(uint8_t v) {
    if (v <= 0x7f) { put(v); }
    else { put(UINT8_FMT); put(v); }
  }

  void write_uint16(uint16_t v) {
    if (v <= 0x7f)       { put(static_cast<uint8_t>(v)); }
    else if (v <= 0xff)  { put(UINT8_FMT); put(static_cast<uint8_t>(v)); }
    else                 { put(UINT16_FMT); put16(v); }
  }

  void write_uint32(uint32_t v) {
    if (v <= 0x7f)       { put(static_cast<uint8_t>(v)); }
    else if (v <= 0xff)  { put(UINT8_FMT); put(static_cast<uint8_t>(v)); }
    else if (v <= 0xffff){ put(UINT16_FMT); put16(static_cast<uint16_t>(v)); }
    else                 { put(UINT32_FMT); put32(v); }
  }

  void write_bin(etl::span<const uint8_t> data) {
    const size_t len = data.size();
    if (len <= 0xff) { put(BIN8); put(static_cast<uint8_t>(len)); }
    else             { put(BIN16); put16(static_cast<uint16_t>(len)); }
    write_bytes(data.data(), len);
  }

  void write_str(const char* s, size_t len) {
    if (len <= 31)       { put(static_cast<uint8_t>(FIXSTR_MASK | len)); }
    else if (len <= 0xff){ put(STR8); put(static_cast<uint8_t>(len)); }
    else                 { _ok = false; return; }
    write_bytes(reinterpret_cast<const uint8_t*>(s), len);
  }

 private:
  void put(uint8_t byte) {
    if (_pos < _cap) { _buf[_pos++] = byte; }
    else { _ok = false; }
  }
  void put16(uint16_t v) { put(static_cast<uint8_t>(v >> 8)); put(static_cast<uint8_t>(v & 0xff)); }
  void put32(uint32_t v) { put16(static_cast<uint16_t>(v >> 16)); put16(static_cast<uint16_t>(v & 0xffff)); }
  void write_bytes(const uint8_t* data, size_t len) {
    if (_pos + len <= _cap) { memcpy(_buf + _pos, data, len); _pos += len; }
    else { _ok = false; }
  }

  uint8_t* _buf;
  size_t   _cap;
  size_t   _pos = 0;
  bool     _ok  = true;
};

// ─── Decoder ────────────────────────────────────────────────────────────────
class Decoder {
 public:
  Decoder(const uint8_t* buf, size_t len) : _buf(buf), _len(len) {}
  explicit Decoder(etl::span<const uint8_t> buf) : _buf(buf.data()), _len(buf.size()) {}

  bool ok() const { return _ok; }
  size_t remaining() const { return _ok ? (_len - _pos) : 0; }

  uint8_t read_array() {
    const uint8_t b = get();
    if ((b & 0xf0) == FIXARRAY_MASK) { return static_cast<uint8_t>(b & 0x0f); }
    _ok = false;
    return 0;
  }

  uint8_t read_uint8() { return static_cast<uint8_t>(read_uint32()); }

  uint16_t read_uint16() { return static_cast<uint16_t>(read_uint32()); }

  uint32_t read_uint32() {
    const uint8_t b = get();
    if (b <= 0x7f) { return b; }
    if (b == UINT8_FMT)  { return get(); }
    if (b == UINT16_FMT) { return get16(); }
    if (b == UINT32_FMT) { return get32(); }
    _ok = false;
    return 0;
  }

  /** Read bin field into provided span, shrinking span to actual length. */
  void read_bin(etl::span<uint8_t>& dst) {
    const size_t len = read_bin_length();
    if (!_ok) { dst = {}; return; }
    if (len > dst.size() || _pos + len > _len) { _ok = false; dst = {}; return; }
    memcpy(dst.data(), _buf + _pos, len);
    _pos += len;
    dst = dst.first(len);
  }

  /** Read bin field, returning a zero-copy view into the source buffer.
   *  WARNING: The returned span's lifetime is tied to the source buffer.
   *  Consume the data before the underlying frame buffer is overwritten. */
  [[nodiscard]] etl::span<const uint8_t> read_bin_view() {
    const size_t len = read_bin_length();
    if (!_ok || _pos + len > _len) { _ok = false; return {}; }
    const auto result = etl::span<const uint8_t>(_buf + _pos, len);
    _pos += len;
    return result;
  }

  /** Read str field into a char buffer with null-termination. Returns chars written. */
  size_t read_str(char* dst, size_t dst_size) {
    const size_t len = read_str_length();
    if (!_ok) { return 0; }
    if (dst_size == 0) { skip(len); return 0; }
    const size_t copy_len = etl::min(len, dst_size - 1);
    if (_pos + len > _len) { _ok = false; return 0; }
    memcpy(dst, _buf + _pos, copy_len);
    dst[copy_len] = '\0';
    _pos += len;
    return copy_len;
  }

 private:
  uint8_t get() {
    if (_pos < _len) { return _buf[_pos++]; }
    _ok = false;
    return 0;
  }
  uint16_t get16() { const uint16_t hi = get(); return static_cast<uint16_t>((hi << 8) | get()); }
  uint32_t get32() { const uint32_t hi = get16(); return (hi << 16) | get16(); }

  size_t read_bin_length() {
    const uint8_t b = get();
    if (b == BIN8)  { return get(); }
    if (b == BIN16) { return get16(); }
    _ok = false;
    return 0;
  }

  size_t read_str_length() {
    const uint8_t b = get();
    if ((b & 0xe0) == static_cast<uint8_t>(FIXSTR_MASK & 0xe0)) { return static_cast<uint8_t>(b & 0x1f); }
    if (b == STR8) { return get(); }
    _ok = false;
    return 0;
  }

  void skip(size_t n) {
    if (_pos + n <= _len) { _pos += n; }
    else { _ok = false; }
  }

  const uint8_t* _buf;
  size_t         _len;
  size_t         _pos = 0;
  bool           _ok  = true;
};

}  // namespace msgpack

#endif  // MSGPACK_CODEC_H
