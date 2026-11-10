#include "services/Process.h"

#include <etl/algorithm.h>
#include <etl/string.h>
#include <pw_span/span.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_PROCESS

namespace {

constexpr int32_t kProcessInvalidPid = -1;

}  // namespace

void ProcessClass::runAsync(etl::string_view cmd,
                            pw::span<const etl::string_view> args,
                            typename ProcessClass::ProcessRunHandler handler) {
  if (handler.is_valid() && Process._pending_run_async.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_RESOURCE_EXHAUSTED);
    handler(kProcessInvalidPid);
    return;
  }

  etl::string<rpc::MAX_PAYLOAD_SIZE> command_buffer;
  bool ok = (cmd.size() <= command_buffer.available());
  if (ok) {
    command_buffer.append(cmd.begin(), cmd.end());
    ok = etl::all_of(args.begin(), args.end(), [&](const auto& arg) {
      if (1U + arg.size() > command_buffer.available()) {
        return false;
      }
      command_buffer.append(" ");
      command_buffer.append(arg.begin(), arg.end());
      return true;
    });
  }

  if (!ok) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_INVALID_ARGUMENT);
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  rpc_pb_ProcessRunAsync p = rpc_pb_ProcessRunAsync_init_default;
  bridge::pb::copy_string(
      p.command, sizeof(p.command),
      etl::string_view(command_buffer.data(), command_buffer.size()));

  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }
  Process._run_call = Bridge.processClient().RunAsync(
      p, [](const rpc_pb_ProcessRunAsyncResponse& resp, pw::Status status) {
        static_cast<void>(status);
        Process._onRunAsyncResponse(resp);
      });

  if (handler.is_valid()) Process._pending_run_async.push(handler);
}

void ProcessClass::poll(int32_t pid,
                        typename ProcessClass::ProcessPollHandler handler) {
  if (handler.is_valid() && _pending_polls.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_RESOURCE_EXHAUSTED);
    return;
  }

  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }

  rpc_pb_ProcessPoll p = rpc_pb_ProcessPoll_init_default;
  p.pid = static_cast<uint32_t>(pid);

  _poll_call = Bridge.processClient().Poll(
      p, [](const rpc_pb_ProcessPollResponse& resp, pw::Status status) {
        static_cast<void>(status);
        Process._onPollResponse(resp);
      });

  if (handler.is_valid()) {
    _pending_polls.push(PendingPoll{pid, handler});
  }
}

void ProcessClass::kill(int32_t pid) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }
  rpc_pb_ProcessKill p = rpc_pb_ProcessKill_init_default;
  p.pid = static_cast<uint32_t>(pid);

  (void)Bridge.processClient().Kill(p);
}

void ProcessClass::_onRunAsyncResponse(
    const rpc_pb_ProcessRunAsyncResponse& msg) {
  if (!_pending_run_async.empty()) {
    auto handler = _pending_run_async.front();
    _pending_run_async.pop();
    handler(static_cast<int32_t>(msg.pid));
  }
}

void ProcessClass::_onPollResponse(const rpc_pb_ProcessPollResponse& msg) {
  if (!_pending_polls.empty()) {
    auto pending = _pending_polls.front();
    _pending_polls.pop();
    pending.second(
        static_cast<rpc::StatusCode>(msg.status), msg.exit_code,
        pw::span<const uint8_t>(msg.stdout_data.bytes, msg.stdout_data.size),
        pw::span<const uint8_t>(msg.stderr_data.bytes, msg.stderr_data.size));
  }
}

void ProcessClass::reset() {
  _run_call = {};
  _poll_call = {};
  _pending_run_async.clear();
  _pending_polls.clear();
}

ProcessType Process;

#endif
