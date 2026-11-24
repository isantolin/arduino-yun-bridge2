#ifndef SERVICES_PROCESS_H
#define SERVICES_PROCESS_H

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

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
  using ProcessPollHandler = etl::delegate<void(
      pw::Status, uint16_t, pw::span<const uint8_t>, pw::span<const uint8_t>)>;
  using PendingPoll = etl::pair<int32_t, ProcessPollHandler>;

  ProcessClass() = default;
  static void runAsync(etl::string_view cmd,
                       pw::span<const etl::string_view> args,
                       ProcessRunHandler handler);
  void poll(int32_t pid, ProcessPollHandler handler);
  static void kill(int32_t pid);

  void _onRunAsyncResponse(const rpc_pb_ProcessRunAsyncResponse& msg,
                           pw::Status status = pw::OkStatus());
  void _onRunAsyncError(pw::Status status);
  void _onPollResponse(const rpc_pb_ProcessPollResponse& msg,
                       pw::Status status = pw::OkStatus());
  void _onPollError(pw::Status status);
  void reset();

  void onLost() override { reset(); }

 private:
  static_assert(bridge::config::MAX_PENDING_PROCESS_POLLS >= 1U);
  pw::rpc::NanopbUnaryReceiver<rpc_pb_ProcessRunAsyncResponse> _run_call{};
  pw::rpc::NanopbUnaryReceiver<rpc_pb_ProcessPollResponse> _poll_call{};
  ProcessRunHandler _pending_run_handler{};
  ProcessPollHandler _pending_poll_handler{};
};

using ProcessType = ProcessClass;
extern ProcessType Process;

#endif
