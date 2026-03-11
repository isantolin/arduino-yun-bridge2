#ifndef RPC_V3_WINDOW_H
#define RPC_V3_WINDOW_H

#include <stdint.h>
#include <stddef.h>

#include "etl/array.h"
#include "etl/span.h"
#include "etl/bitset.h"

namespace rpc {
namespace v3 {

// V3 Sliding Window Constants
constexpr uint8_t WINDOW_SIZE = 16;
constexpr uint8_t WINDOW_MASK = 0x0F;

// Simulated window structure for Arduino (Zero-allocation)
// Only stores the length of payloads for offset indexing, 
// data resides in a contiguous buffer block.
struct SlidingWindow {
    etl::array<uint16_t, WINDOW_SIZE> packet_lengths;
    etl::bitset<WINDOW_SIZE> valid_packets;
    
    uint8_t head = 0; // Next to send
    uint8_t tail = 0; // Last un-acked
    
    void clear() {
        packet_lengths.fill(0);
        valid_packets.reset();
        head = 0;
        tail = 0;
    }
    
    bool is_full() const {
        return ((head + 1) & WINDOW_MASK) == tail;
    }
    
    bool is_empty() const {
        return head == tail;
    }
    
    uint8_t pending_count() const {
        return (head - tail) & WINDOW_MASK;
    }
};

} // namespace v3
} // namespace rpc

#endif // RPC_V3_WINDOW_H