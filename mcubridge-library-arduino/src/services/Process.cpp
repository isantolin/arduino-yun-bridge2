#include "services/Process.h"

#include <etl/algorithm.h>
#include <etl/string.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_PROCESS

namespace {

constexpr int32_t kProcessInvalidPid = -1;

}  // namespace

ProcessClass::ProcessClass() {}

void ProcessClass::runAsync(etl::string_view cmd,
                            etl::span<const etl::string_view> args,
                            typename ProcessClass::ProcessRunHandler handler) {
  if (handler.is_valid() && Process._pending_run_async.full()) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_LIMIT_REACHED));
    handler(kProcessInvalidPid);
    return;
  }

  etl::string<rpc::MAX_PAYLOAD_SIZE> command_buffer;
  bool ok = true;
  if (cmd.size() <= command_buffer.available()) {
    command_buffer.append(cmd.begin(), cmd.end());
  } else {
    ok = false;
  }

  etl::for_each(args.begin(), args.end(), [&](const auto& arg) {
    if (!ok) return;
    if (1U + arg.size() > command_buffer.available()) {
      ok = false;
      return;
    }
    command_buffer.append(" ");
    command_buffer.append(arg.begin(), arg.end());
  });

  if (!ok) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_RUN_ASYNC_FAILED));
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  rpc::payload::ProcessRunAsync p = {};
  rpc::Payload::copy_to_pb_string(
      p.command,
      etl::string_view(command_buffer.data(), command_buffer.size()));

  if (!Bridge.sendOrEmitStatus(
          rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 0, p,
          etl::string_view(rpc::status_reason::PROCESS_RUN_ASYNC_FAILED))) {
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  if (handler.is_valid()) Process._pending_run_async.push(handler);
}

void ProcessClass::poll(int32_t pid,
                        typename ProcessClass::ProcessPollHandler handler) {
  if (handler.is_valid() && _pending_polls.full()) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_LIMIT_REACHED));
    return;
  }

  rpc::payload::ProcessPoll p = {};
  p.pid = static_cast<uint32_t>(pid);

  if (!Bridge.sendOrEmitStatus(
          rpc::CommandId::CMD_PROCESS_POLL, 0, p,
          etl::string_view(rpc::status_reason::PROCESS_RUN_INTERNAL_ERROR))) {
    return;
  }

  if (handler.is_valid()) {
    _pending_polls.push(PendingPoll{pid, handler});
  }
}

void ProcessClass::kill(int32_t pid) {
  rpc::payload::ProcessKill p = {};
  p.pid = static_cast<uint32_t>(pid);
  Bridge.sendOrEmitStatus(
      rpc::CommandId::CMD_PROCESS_KILL, 0, p,
      etl::string_view(rpc::status_reason::PROCESS_KILL_FAILED));
}

void ProcessClass::_onRunAsyncResponse(
    const rpc::payload::ProcessRunAsyncResponse& msg) {
  _pending_run_async.dispatch(
      msg, [](const ProcessRunHandler& handler,
              const rpc::payload::ProcessRunAsyncResponse& response) {
        handler(static_cast<int32_t>(response.pid));
      });
}

void ProcessClass::_onPollResponse(
    const rpc::payload::ProcessPollResponse& msg) {
  _pending_polls.dispatch(
      msg, [](const PendingPoll& pending,
              const rpc::payload::ProcessPollResponse& response) {
        pending.second(static_cast<rpc::StatusCode>(response.status),
                       response.exit_code,
                       etl::span<const uint8_t>(response.stdout_data.bytes,
                                                response.stdout_data.size),
                       etl::span<const uint8_t>(response.stderr_data.bytes,
                                                response.stderr_data.size));
      });
}

void ProcessClass::reset() {
  _pending_run_async.clear();
  _pending_polls.clear();
}

ProcessType Process;

#endif
