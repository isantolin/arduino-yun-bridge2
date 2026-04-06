#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const T& msg) {
    msgpack::Encoder enc(const_cast<uint8_t*>(frame.payload.data()), frame.payload.size());
    if (msg.encode(enc)) {
        frame.header.payload_length = static_cast<uint16_t>(enc.size());
    }
}

} // namespace test
} // namespace bridge

#endif
