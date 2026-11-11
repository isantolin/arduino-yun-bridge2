#ifndef SERVICES_PROCESS_H
#define SERVICES_PROCESS_H

#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/string_view.h>
#include <etl/utility.h>
#include <pw_span/span.h>

#include "Bridge.h"
#include "protocol/mcu_wire.pb.h"
#include "protocol/rpc_protocol.h"

class ProcessClass : public bridge::BridgeObserver {
 public:
  using ProcessRunHandler = etl::delegate<void(int32_t)>;
  using ProcessPollHandler =
      etl::delegate<void(rpc::StatusCode, uint16_t, pw::span<const uint8_t>,
                         pw::span<const uint8_t>)>;
  using PendingPoll = etl::pair<int32_t, ProcessPollHandler>;

  ProcessClass() = default;
  static void runAsync(etl::string_view cmd,
                       pw::span<const etl::string_view> args,
                       ProcessRunHandler handler);
  void poll(int32_t pid, ProcessPollHandler handler);
  static void kill(int32_t pid);

  void _onRunAsyncResponse(const rpc_pb_ProcessRunAsyncResponse& msg);
  void _onPollResponse(const rpc_pb_ProcessPollResponse& msg);
  void reset();

  void onLost() override { reset(); }

 private:
  pw::rpc::NanopbUnaryReceiver<rpc_pb_ProcessRunAsyncResponse> _run_call{};
  pw::rpc::NanopbUnaryReceiver<rpc_pb_ProcessPollResponse> _poll_call{};
  etl::circular_buffer<ProcessRunHandler,
                       bridge::config::MAX_PENDING_PROCESS_POLLS>
      _pending_run_async;
  etl::circular_buffer<PendingPoll, bridge::config::MAX_PENDING_PROCESS_POLLS>
      _pending_polls;
};

using ProcessType = ProcessClass;
extern ProcessType Process;

#endif
