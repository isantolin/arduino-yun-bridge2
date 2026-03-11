#ifndef RPC_V3_ROUTER_H
#define RPC_V3_ROUTER_H

#include <stdint.h>
#include <stddef.h>

#include "etl/queue.h"
#include "etl/span.h"

// Re-use V3 Header definition
#include "rpc_v3_frame.h"

namespace rpc {
namespace v3 {

// Pre-allocated slots per endpoint type based on SLA requirements
// SYS needs to never block. BULK can tolerate delays.
constexpr size_t Q_SYS_SIZE  = 4;
constexpr size_t Q_CTRL_SIZE = 8;
constexpr size_t Q_DATA_SIZE = 4;
constexpr size_t Q_BULK_SIZE = 2;

struct V3PendingFrame {
    Header header;
    // Just a stub for PoC. In reality this points to an offset in a circular buffer.
    uint16_t offset; 
    uint16_t length;
};

// Priority Router using multiple queues
class V3PriorityRouter {
private:
    etl::queue<V3PendingFrame, Q_SYS_SIZE>  sys_queue;
    etl::queue<V3PendingFrame, Q_CTRL_SIZE> ctrl_queue;
    etl::queue<V3PendingFrame, Q_DATA_SIZE> data_queue;
    etl::queue<V3PendingFrame, Q_BULK_SIZE> bulk_queue;

public:
    // O(1) Routing into proper priority queues based on Header
    bool route_incoming(const Header& hdr, uint16_t offset, uint16_t len) {
        V3PendingFrame frame = {hdr, offset, len};
        
        switch (hdr.endpoint) {
            case EP_SYS:
                if (sys_queue.full()) return false;
                sys_queue.push(frame);
                return true;
            case EP_CTRL:
                if (ctrl_queue.full()) return false;
                ctrl_queue.push(frame);
                return true;
            case EP_DATA:
                if (data_queue.full()) return false;
                data_queue.push(frame);
                return true;
            case EP_BULK:
                if (bulk_queue.full()) return false;
                bulk_queue.push(frame);
                return true;
            default:
                return false;
        }
    }

    // O(1) Dequeue ensuring SYS > CTRL > DATA > BULK priorities
    bool dequeue_highest_priority(V3PendingFrame& out_frame) {
        if (!sys_queue.empty()) {
            out_frame = sys_queue.front();
            sys_queue.pop();
            return true;
        }
        if (!ctrl_queue.empty()) {
            out_frame = ctrl_queue.front();
            ctrl_queue.pop();
            return true;
        }
        if (!data_queue.empty()) {
            out_frame = data_queue.front();
            data_queue.pop();
            return true;
        }
        if (!bulk_queue.empty()) {
            out_frame = bulk_queue.front();
            bulk_queue.pop();
            return true;
        }
        return false;
    }
    
    // Safety reporting
    bool is_congested() const {
        // Warning threshold if CTRL or SYS are near full capacity
        return sys_queue.available() <= 1 || ctrl_queue.available() <= 2;
    }
};

} // namespace v3
} // namespace rpc

#endif // RPC_V3_ROUTER_H