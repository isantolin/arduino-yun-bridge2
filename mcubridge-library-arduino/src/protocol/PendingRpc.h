#ifndef PROTOCOL_PENDING_RPC_H
#define PROTOCOL_PENDING_RPC_H

#undef min
#undef max
#include <etl/delegate.h>
#include <etl/queue.h>
#include <stddef.h>
#include <stdint.h>

namespace bridge {

/**
 * @brief Zero-Heap C++17 template managing pending RPC requests and responses.
 *
 * Unifies queue bounds, capacity checking, typed callback dispatch, and
 * link disconnection resets across MCU Bridge services.
 */
template <typename RespMsg, size_t Capacity,
          typename Handler = etl::delegate<void(const RespMsg&)>>
class PendingRpcManager {
 public:
  using MessageType = RespMsg;
  using HandlerType = Handler;

  PendingRpcManager() = default;

  [[nodiscard]] bool full() const { return _queue.full(); }
  [[nodiscard]] bool empty() const { return _queue.empty(); }

  bool push(const Handler& handler) {
    if (_queue.full()) {
      return false;
    }
    _queue.push(handler);
    return true;
  }

  void pop() { _queue.pop(); }

  Handler& front() { return _queue.front(); }
  const Handler& front() const { return _queue.front(); }

  void clear() { _queue.clear(); }

  template <typename Invoker>
  bool dispatch(const RespMsg& msg, Invoker&& invoker) {
    if (_queue.empty()) {
      return false;
    }
    Handler handler = _queue.front();
    _queue.pop();
    invoker(handler, msg);
    return true;
  }

 private:
  etl::queue<Handler, Capacity> _queue;
};

}  // namespace bridge

#endif  // PROTOCOL_PENDING_RPC_H
