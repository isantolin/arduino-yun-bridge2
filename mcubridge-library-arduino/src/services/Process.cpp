#include "services/Process.h"

#include <etl/algorithm.h>
#include <etl/string.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_PROCESS

namespace {

constexpr int32_t kProcessInvalidPid = -1;

}  // namespace

void ProcessClass::runAsync(etl::string_view cmd,
                            etl::span<const etl::string_view> args,
                            typename ProcessClass::ProcessRunHandler handler) {
  if (handler.is_valid() && Process._pending_run_async.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
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
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  rpc_pb_ProcessRunAsync p = rpc_pb_ProcessRunAsync_init_default;
  bridge::pb::copy_string(
      p.command, sizeof(p.command),
      etl::string_view(command_buffer.data(), command_buffer.size()));

  constexpr uint32_t kServiceId = pw::tokenizer::Hash("bridge.ProcessService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("RunAsync");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId,
                             rpc_pb_ProcessRunAsync_fields, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  if (handler.is_valid()) Process._pending_run_async.push(handler);
}

void ProcessClass::poll(int32_t pid,
                        typename ProcessClass::ProcessPollHandler handler) {
  if (handler.is_valid() && _pending_polls.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc_pb_ProcessPoll p = rpc_pb_ProcessPoll_init_default;
  p.pid = static_cast<uint32_t>(pid);

  constexpr uint32_t kServiceId = pw::tokenizer::Hash("bridge.ProcessService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Poll");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_ProcessPoll_fields,
                             p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  if (handler.is_valid()) {
    _pending_polls.push(PendingPoll{pid, handler});
  }
}

void ProcessClass::kill(int32_t pid) {
  rpc_pb_ProcessKill p = rpc_pb_ProcessKill_init_default;
  p.pid = static_cast<uint32_t>(pid);

  constexpr uint32_t kServiceId = pw::tokenizer::Hash("bridge.ProcessService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Kill");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_ProcessKill_fields,
                             p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void ProcessClass::_onRunAsyncResponse(
    const rpc_pb_ProcessRunAsyncResponse& msg) {
  if (!_pending_run_async.empty()) {
    auto handler = _pending_run_async.front();
    _pending_run_async.pop();
    if (handler.is_valid()) {
      handler(static_cast<int32_t>(msg.pid));
    }
  }
}

void ProcessClass::_onPollResponse(const rpc_pb_ProcessPollResponse& msg) {
  if (!_pending_polls.empty()) {
    auto pending = _pending_polls.front();
    _pending_polls.pop();
    if (pending.second.is_valid()) {
      pending.second(
          static_cast<rpc::StatusCode>(msg.status), msg.exit_code,
          etl::span<const uint8_t>(msg.stdout_data.bytes, msg.stdout_data.size),
          etl::span<const uint8_t>(msg.stderr_data.bytes,
                                   msg.stderr_data.size));
    }
  }
}

void ProcessClass::reset() {
  _pending_run_async.clear();
  _pending_polls.clear();
}

ProcessType Process;

#endif
