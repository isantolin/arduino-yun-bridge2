/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 *
 * MsgPack encoder/decoder using mpack library [SIL-2].
 * Zero-heap, static buffer based implementation.
 */
#ifndef MSGPACK_CODEC_H
#define MSGPACK_CODEC_H

#include <etl/span.h>
#include <mpack.h>
#include <stddef.h>
#include <stdint.h>

namespace msgpack {

// ─── Encoder ────────────────────────────────────────────────────────────────
class Encoder {
 public:
  Encoder(uint8_t* buf, size_t cap) {
    mpack_writer_init(&_writer, reinterpret_cast<char*>(buf), cap);
  }
  explicit Encoder(etl::span<uint8_t> buf) : Encoder(buf.data(), buf.size()) {}

  bool ok() const { return mpack_writer_error(&_writer) == mpack_ok; }
  size_t size() const { return mpack_writer_buffer_used(&_writer); }
  
  etl::span<const uint8_t> result() const {
    // In mpack_writer_t, 'buffer' is a public member pointing to the start.
    return {reinterpret_cast<const uint8_t*>(_writer.buffer), size()};
  }

  void write_array(uint32_t count) { mpack_start_array(&_writer, count); }
  
  void write_uint8(uint8_t v) { mpack_write_u8(&_writer, v); }
  void write_uint16(uint16_t v) { mpack_write_u16(&_writer, v); }
  void write_uint32(uint32_t v) { mpack_write_u32(&_writer, v); }

  void write_bin(etl::span<const uint8_t> data) {
    mpack_write_bin(&_writer, reinterpret_cast<const char*>(data.data()), 
                    static_cast<uint32_t>(data.size()));
  }

  void write_str(const char* s, uint32_t len) {
    mpack_write_str(&_writer, s, len);
  }

 private:
  mutable mpack_writer_t _writer;
};

// ─── Decoder ────────────────────────────────────────────────────────────────
class Decoder {
 public:
  Decoder(const uint8_t* buf, size_t len) {
    mpack_reader_init_data(&_reader, reinterpret_cast<const char*>(buf), len);
  }
  explicit Decoder(etl::span<const uint8_t> buf)
      : Decoder(buf.data(), buf.size()) {}

  bool ok() const { return mpack_reader_error(&_reader) == mpack_ok; }

  uint32_t read_array() { return mpack_expect_array(&_reader); }

  uint8_t read_uint8() { return mpack_expect_u8(&_reader); }
  uint16_t read_uint16() { return mpack_expect_u16(&_reader); }
  uint32_t read_uint32() { return mpack_expect_u32(&_reader); }

  [[nodiscard]] etl::span<const uint8_t> read_bin_view() {
    uint32_t len = mpack_expect_bin(&_reader);
    if (!ok()) return {};
    const char* data = mpack_read_bytes_inplace(&_reader, len);
    mpack_done_bin(&_reader);
    if (!data) return {};
    return {reinterpret_cast<const uint8_t*>(data), static_cast<size_t>(len)};
  }

  [[nodiscard]] etl::span<const char> read_str_view() {
    uint32_t len = mpack_expect_str(&_reader);
    if (!ok()) return {};
    const char* data = mpack_read_bytes_inplace(&_reader, len);
    mpack_done_str(&_reader);
    if (!data) return {};
    return {data, static_cast<size_t>(len)};
  }

 private:
  mutable mpack_reader_t _reader;
};

}  // namespace msgpack

#endif
