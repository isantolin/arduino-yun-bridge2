#ifndef SERVICES_PROCESS_H
#define SERVICES_PROCESS_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/delegate.h>
#include <etl/queue.h>
#include <etl/span.h>
#include <etl/string_view.h>

#include "protocol/BridgeEvents.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

class ProcessClass : public BridgeObserver {
 public:
  using ProcessRunHandler = etl::delegate<void(int32_t)>;
  using ProcessPollHandler =
      etl::delegate<void(rpc::StatusCode, uint16_t, etl::span<const uint8_t>,
                         etl::span<const uint8_t>)>;

  ProcessClass();
  static void runAsync(etl::string_view cmd,
                       etl::span<const etl::string_view> args,
                       ProcessRunHandler handler);
  void poll(int32_t pid, ProcessPollHandler handler);
  static void kill(int32_t pid);

  void _onKillNotification(const rpc_pb_ProcessKill& msg);
  void _onRunAsyncResponse(const rpc_pb_ProcessRunAsyncResponse& msg);
  void _onPollResponse(const rpc_pb_ProcessPollResponse& msg);
  void reset();

  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { reset(); }

  struct PendingRunAsync {
    ProcessRunHandler handler;
  };
  struct PendingPoll {
    int32_t pid;
    ProcessPollHandler handler;
  };
  etl::queue<PendingRunAsync, bridge::config::MAX_PENDING_PROCESS_POLLS>
      _pending_run_async;
  etl::queue<PendingPoll, bridge::config::MAX_PENDING_PROCESS_POLLS>
      _pending_polls;
};

extern ProcessClass Process;

#endif
