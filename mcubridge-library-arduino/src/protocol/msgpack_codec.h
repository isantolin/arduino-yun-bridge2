/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 *
 * Minimal MsgPack encoder/decoder for embedded targets.
 * Supports only the subset used by the MCU Bridge protocol:
 *   fixarray, array16/32, fixint/uint8/uint16/uint32, bin8/16/32,
 * fixstr/str8/16/32. Header-only, no heap allocation, ETL byte-stream based.
 */
#ifndef MSGPACK_CODEC_H
#define MSGPACK_CODEC_H

#include <etl/algorithm.h>
#include <etl/byte_stream.h>
#include <etl/span.h>
#include <stddef.h>
#include <stdint.h>

#include "rpc_protocol.h"

namespace msgpack {

// ─── Encoder ────────────────────────────────────────────────────────────────
class Encoder {
 public:
  Encoder(uint8_t* buf, size_t cap)
      : _writer(buf, buf + cap, etl::endian::big) {}
  explicit Encoder(etl::span<uint8_t> buf) : _writer(buf, etl::endian::big) {}

  bool ok() const { return _ok; }
  size_t size() const { return _writer.size_bytes(); }
  etl::span<const uint8_t> result() const {
    auto d = _writer.used_data();
    return {reinterpret_cast<const uint8_t*>(d.data()), d.size()};
  }

  void write_array(uint8_t count) {
    if (count <= rpc::MSGPACK_FIXARRAY_VALUE_MASK) {
      put(static_cast<uint8_t>(rpc::MSGPACK_FIXARRAY_MASK | count));
    } else {
      put(0xDC);  // array16
      put_multi(static_cast<uint16_t>(count));
    }
  }

  template <typename T>
  void write_uint(T v) {
    if (v <= rpc::MSGPACK_POSITIVE_FIXINT_MAX) {
      put(static_cast<uint8_t>(v));
    } else if (v <= rpc::MSGPACK_UINT8_MAX_VAL) {
      put(rpc::MSGPACK_UINT8_FMT);
      put(static_cast<uint8_t>(v));
    } else if constexpr (sizeof(T) >= 2) {
      if constexpr (sizeof(T) == 2) {
        put(rpc::MSGPACK_UINT16_FMT);
        put_multi(static_cast<uint16_t>(v));
      } else {
        if (v <= rpc::MSGPACK_UINT16_MAX_VAL) {
          put(rpc::MSGPACK_UINT16_FMT);
          put_multi(static_cast<uint16_t>(v));
        } else {
          put(rpc::MSGPACK_UINT32_FMT);
          put_multi(static_cast<uint32_t>(v));
        }
      }
    }
  }

  void write_uint8(uint8_t v) { write_uint(v); }
  void write_uint16(uint16_t v) { write_uint(v); }
  void write_uint32(uint32_t v) { write_uint(v); }

  void write_bin(etl::span<const uint8_t> data) {
    const size_t len = data.size();
    if (len <= 255) {
      put(0xC4);  // bin8
      put(static_cast<uint8_t>(len));
    } else if (len <= 65535) {
      put(0xC5);  // bin16
      put_multi(static_cast<uint16_t>(len));
    } else {
      put(0xC6);  // bin32
      put_multi(static_cast<uint32_t>(len));
    }
    write_bytes(data.data(), len);
  }

  void write_str(const char* s, size_t len) {
    if (len <= 31) {
      put(static_cast<uint8_t>(0xA0 | len));
    } else if (len <= 255) {
      put(0xD9);  // str8
      put(static_cast<uint8_t>(len));
    } else if (len <= 65535) {
      put(0xDA);  // str16
      put_multi(static_cast<uint16_t>(len));
    } else {
      put(0xDB);  // str32
      put_multi(static_cast<uint32_t>(len));
    }
    write_bytes(reinterpret_cast<const uint8_t*>(s), len);
  }

 private:
  void put(uint8_t byte) {
    if (!_ok || !_writer.write(byte)) {
      _ok = false;
    }
  }

  template <typename T>
  void put_multi(T value) {
    if (!_ok || !_writer.write(value)) {
      _ok = false;
    }
  }

  void write_bytes(const uint8_t* data, size_t len) {
    if (!_ok || !_writer.write(etl::span<const uint8_t>(data, len))) {
      _ok = false;
    }
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

  uint32_t read_array() {
    const uint8_t b = get();
    if ((b & 0xF0) == 0x90) {
      return b & 0x0F;
    }
    if (b == 0xDC) {
      return get_multi<uint16_t>();
    }
    if (b == 0xDD) {
      return get_multi<uint32_t>();
    }
    _ok = false;
    return 0;
  }

  uint8_t read_uint8() { return static_cast<uint8_t>(read_uint32()); }
  uint16_t read_uint16() { return static_cast<uint16_t>(read_uint32()); }

  uint32_t read_uint32() {
    const uint8_t b = get();
    if (b <= 0x7F) {
      return b;
    }
    if (b == 0xCC) {
      return get();
    }
    if (b == 0xCD) {
      return get_multi<uint16_t>();
    }
    if (b == 0xCE) {
      return get_multi<uint32_t>();
    }
    _ok = false;
    return 0;
  }

  [[nodiscard]] etl::span<const uint8_t> read_bin_view() {
    const size_t len = read_data_length();
    if (!_ok || _reader.available_bytes() < len) {
      _ok = false;
      return {};
    }
    auto view = _reader.read<uint8_t>(len);
    if (!view.has_value()) {
      _ok = false;
      return {};
    }
    return view.value();
  }

  [[nodiscard]] etl::span<const char> read_str_view() {
    const size_t len = read_data_length();
    if (!_ok || _reader.available_bytes() < len) {
      _ok = false;
      return {};
    }
    auto view = _reader.read<uint8_t>(len);
    if (!view.has_value()) {
      _ok = false;
      return {};
    }
    return {reinterpret_cast<const char*>(view.value().data()),
            view.value().size()};
  }

 private:
  uint8_t get() {
    if (!_ok) return 0;
    auto opt = _reader.read<uint8_t>();
    if (opt.has_value()) {
      return opt.value();
    }
    _ok = false;
    return 0;
  }

  template <typename T>
  T get_multi() {
    if (!_ok) return 0;
    auto opt = _reader.read<T>();
    if (opt.has_value()) {
      return opt.value();
    }
    _ok = false;
    return 0;
  }

  size_t read_data_length() {
    const uint8_t b = get();
    if ((b & 0xE0) == 0xA0) {
      return b & 0x1F;
    }  // fixstr
    if (b == 0xD9 || b == 0xC4) {
      return get();
    }  // str8, bin8
    if (b == 0xDA || b == 0xC5) {
      return get_multi<uint16_t>();
    }  // str16, bin16
    if (b == 0xDB || b == 0xC6) {
      return get_multi<uint32_t>();
    }  // str32, bin32
    _ok = false;
    return 0;
  }

  etl::byte_stream_reader _reader;
  bool _ok = true;
};

}  // namespace msgpack

#endif
