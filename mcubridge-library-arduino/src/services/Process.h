#ifndef SERVICES_PROCESS_H
#define SERVICES_PROCESS_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/queue.h>
#include <etl/delegate.h>
#include <etl/string_view.h>
#include <etl/span.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

class ProcessClass {
 public:
  using ProcessPollHandler = etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>;

  ProcessClass();
  [[maybe_unused]] static void runAsync(etl::string_view cmd, etl::span<const etl::string_view> args, etl::delegate<void(int32_t)> handler);
  [[maybe_unused]] void poll(int32_t pid, ProcessPollHandler handler);
  [[maybe_unused]] static void kill(int32_t pid);

  static void _kill(const rpc::payload::ProcessKill& msg);
  static void _onRunAsyncResponse(const rpc::payload::ProcessRunAsyncResponse& msg);
  static void _onPollResponse(const rpc::payload::ProcessPollResponse& msg);
  void reset();

  static void notification(MsgBridgeSynchronized) { /* ready */ }
  void notification(MsgBridgeLost) { reset(); }

  struct PendingPoll { int32_t pid; };
  etl::queue<PendingPoll, bridge::config::MAX_PENDING_PROCESS_POLLS> _pending_polls;
};

extern ProcessClass Process;

#endif
