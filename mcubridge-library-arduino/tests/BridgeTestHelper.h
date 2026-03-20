#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"
#include "nanopb/pb_encode.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const T& msg) {
    pb_ostream_t out_stream = pb_ostream_from_buffer(const_cast<uint8_t*>(frame.payload.data()), frame.payload.size());
    if (pb_encode(&out_stream, rpc::Payload::Descriptor<T>::fields(), &msg)) {
        frame.header.payload_length = static_cast<uint16_t>(out_stream.bytes_written);
    }
}

} // namespace test
} // namespace bridge

#endif
