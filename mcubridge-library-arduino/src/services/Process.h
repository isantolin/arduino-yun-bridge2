#ifndef SERVICES_PROCESS_H
#define SERVICES_PROCESS_H

#include <stdint.h>
#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/delegate.h>
#include <etl/queue.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge { namespace test { class ProcessTestAccessor; } }
#endif

class ProcessClass : public BridgeObserver {
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::ProcessTestAccessor;
#endif
 public:
  using ProcessRunAsyncHandler = etl::delegate<void(int16_t)>;
  using ProcessPollHandler = etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>;
  using ProcessKillHandler = etl::delegate<void(rpc::StatusCode)>;

  ProcessClass();

  // [SIL-2] Observer Interface
  [[maybe_unused]] void notification(MsgBridgeSynchronized) override { /* ready */ }
  [[maybe_unused]] void notification(MsgBridgeLost) override { reset(); }

  [[maybe_unused]] void runAsync(etl::string_view command, etl::span<const etl::string_view> args, ProcessRunAsyncHandler handler);
  [[maybe_unused]] void poll(int16_t pid, ProcessPollHandler handler);
  [[maybe_unused]] void kill(int16_t pid, ProcessKillHandler handler = {});
  bool _kill(uint32_t pid);
  void reset();

  void _onRunAsyncResponse(const rpc::payload::ProcessRunAsyncResponse& msg);
  void _onPollResponse(const rpc::payload::ProcessPollResponse& msg, etl::span<const uint8_t> stdout_data, etl::span<const uint8_t> stderr_data);

 private:
  struct PendingAsyncRun {
    ProcessRunAsyncHandler handler;
  };

  struct PendingPoll {
    int16_t pid;
    ProcessPollHandler handler;
  };

  // [SIL-2] Use ETL containers for safe queue management
  etl::queue<PendingAsyncRun, bridge::config::MAX_PENDING_PROCESS_POLLS> _pending_async_runs;
  etl::queue<PendingPoll, bridge::config::MAX_PENDING_PROCESS_POLLS> _pending_polls;
};

#if BRIDGE_ENABLE_PROCESS
extern ProcessClass Process;
#endif

#endif
