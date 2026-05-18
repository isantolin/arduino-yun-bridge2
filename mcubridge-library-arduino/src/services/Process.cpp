#include "services/Process.h"

#include <etl/algorithm.h>
#include <etl/array.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_PROCESS

namespace {

constexpr size_t kProcessCommandBufferSize = rpc::MAX_PAYLOAD_SIZE;
constexpr int32_t kProcessInvalidPid = -1;

bool append_token(etl::array<char, kProcessCommandBufferSize>& command_buffer,
                  size_t& write_pos, etl::string_view token,
                  bool prepend_space) {
  const size_t required_space = prepend_space ? 1U : 0U;
  if (write_pos + required_space >= command_buffer.size()) return false;
  if (prepend_space) command_buffer[write_pos++] = ' ';

  const size_t available = command_buffer.size() - write_pos - 1U;
  if (token.size() > available) return false;
  etl::copy_n(token.begin(), token.size(), command_buffer.begin() + write_pos);
  write_pos += token.size();
  return true;
}

}  // namespace

ProcessClass::ProcessClass() {}

void ProcessClass::runAsync(etl::string_view cmd,
                            etl::span<const etl::string_view> args,
                            ProcessRunHandler handler) {
  if (handler.is_valid() && Process._pending_run_async.full()) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_LIMIT_REACHED));
    handler(kProcessInvalidPid);
    return;
  }

  etl::array<char, kProcessCommandBufferSize> command_buffer = {};
  size_t write_pos = 0;
  bool ok = append_token(command_buffer, write_pos, cmd, false);
  etl::for_each(args.begin(), args.end(), [&](etl::string_view arg) {
    if (!ok) return;
    ok = append_token(command_buffer, write_pos, arg, true);
  });
  if (!ok) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_RUN_ASYNC_FAILED));
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }
  command_buffer[write_pos] = rpc::RPC_NULL_TERMINATOR;

  const bool send_ok =
      Bridge.send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 0,
                  rpc::payload::ProcessRunAsync{
                      etl::span<const char>(command_buffer.data(), write_pos)});
  if (!send_ok) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_RUN_ASYNC_FAILED));
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  if (handler.is_valid()) Process._pending_run_async.push({handler});
}

void ProcessClass::poll(int32_t pid, ProcessPollHandler handler) {
  if (handler.is_valid() && _pending_polls.full()) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_LIMIT_REACHED));
    return;
  }

  if (!Bridge.send(rpc::CommandId::CMD_PROCESS_POLL, 0,
                   rpc::payload::ProcessPoll{static_cast<uint32_t>(pid)})) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_RUN_INTERNAL_ERROR));
    return;
  }

  if (handler.is_valid()) {
    _pending_polls.push({pid, handler});
  }
}

void ProcessClass::kill(int32_t pid) {
  (void)Bridge.send(rpc::CommandId::CMD_PROCESS_KILL, 0,
                    rpc::payload::ProcessKill{static_cast<uint32_t>(pid)});
}

void ProcessClass::_kill(const rpc::payload::ProcessKill& msg) {
  kill(static_cast<int32_t>(msg.pid));
}

void ProcessClass::_onRunAsyncResponse(
    const rpc::payload::ProcessRunAsyncResponse& msg) {
  if (_pending_run_async.empty()) return;
  const PendingRunAsync pending = _pending_run_async.front();
  _pending_run_async.pop();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<int32_t>(msg.pid));
  }
}

void ProcessClass::_onPollResponse(
    const rpc::payload::ProcessPollResponse& msg) {
  if (_pending_polls.empty()) return;
  const PendingPoll pending = _pending_polls.front();
  _pending_polls.pop();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<rpc::StatusCode>(msg.status), msg.exit_code,
                    msg.stdout_data, msg.stderr_data);
  }
}

void ProcessClass::reset() {
  _pending_run_async.clear();
  _pending_polls.clear();
}

ProcessClass Process;

#endif
