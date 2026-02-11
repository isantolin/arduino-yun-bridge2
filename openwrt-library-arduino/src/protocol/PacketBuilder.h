#ifndef PACKET_BUILDER_H
#define PACKET_BUILDER_H

#include "rpc_frame.h"
#include "rpc_protocol.h"
#include <etl/vector.h>
#include <etl/string_view.h>
#include <etl/algorithm.h>

namespace rpc {

/**
 * @brief Fluid interface for building RPC frame payloads.
 * [SIL-2 COMPLIANT] Uses static buffers and bounded logic.
 */
class PacketBuilder {
public:
    explicit PacketBuilder(etl::ivector<uint8_t>& payload) 
        : _payload(payload) {
        _payload.clear();
    }

    PacketBuilder& add(uint8_t byte) {
        if (!_payload.full()) {
            _payload.push_back(byte);
        }
        return *this;
    }

    PacketBuilder& add(const uint8_t* data, size_t len) {
        const size_t available = _payload.capacity() - _payload.size();
        const size_t to_copy = etl::min(len, available);
        if (to_copy > 0) {
            _payload.insert(_payload.end(), data, data + to_copy);
        }
        return *this;
    }

    PacketBuilder& add_u16(uint16_t value) {
        uint8_t buf[2];
        write_u16_be(buf, value);
        return add(buf, 2);
    }

    PacketBuilder& add_u32(uint32_t value) {
        uint8_t buf[4];
        write_u32_be(buf, value);
        return add(buf, 4);
    }

    PacketBuilder& add_pascal_string(etl::string_view str) {
        const uint8_t len = static_cast<uint8_t>(etl::min<size_t>(str.length(), 255));
        add(len);
        if (len > 0) {
            add(reinterpret_cast<const uint8_t*>(str.data()), len);
        }
        return *this;
    }

    size_t size() const { return _payload.size(); }
    const uint8_t* data() const { return _payload.data(); }

private:
    etl::ivector<uint8_t>& _payload;
};

} // namespace rpc

#endif // PACKET_BUILDER_H
