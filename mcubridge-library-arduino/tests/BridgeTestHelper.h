#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const T& msg) {
    mpack_writer_t writer;
    mpack_writer_init(&writer, reinterpret_cast<char*>(const_cast<uint8_t*>(frame.payload.data())), frame.payload.size());
    if (msg.encode(&writer)) {
        size_t used = mpack_writer_buffer_used(&writer);
        frame.header.payload_length = static_cast<uint16_t>(used);
        frame.payload = frame.payload.subspan(0, used);
    }
}

} // namespace test
} // namespace bridge

#endif
