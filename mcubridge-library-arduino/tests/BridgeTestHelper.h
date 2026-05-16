#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const T& msg) {
    JsonDocument doc;
    if (msg.encode(doc.to<JsonVariant>())) {
        size_t used = serializeMsgPack(doc, (char*)const_cast<uint8_t*>(frame.payload.data()), frame.payload.size());
        frame.header.payload_length = static_cast<uint16_t>(used);
        frame.payload = frame.payload.subspan(0, used);
    }
}

} // namespace test
} // namespace bridge

#endif
