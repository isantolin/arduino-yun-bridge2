#include "services/Process.h"

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/string.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_PROCESS

namespace {

constexpr size_t kProcessCommandBufferSize = 64U;
constexpr int32_t kProcessInvalidPid = -1;

}  // namespace

template <typename T>
ProcessClass<T>::ProcessClass() {}

template <typename T>
void ProcessClass<T>::runAsync(
    etl::string_view cmd, etl::span<const etl::string_view> args,
    typename ProcessClass<T>::ProcessRunHandler handler) {
  if (handler.is_valid() && Process._pending_run_async.full()) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_LIMIT_REACHED));
    handler(kProcessInvalidPid);
    return;
  }

  etl::string<kProcessCommandBufferSize> command_buffer;
  bool ok = true;
  if (cmd.size() <= command_buffer.available()) {
    command_buffer.append(cmd.begin(), cmd.end());
  } else {
    ok = false;
  }

  etl::for_each(args.begin(), args.end(), [&](etl::string_view arg) {
    if (!ok) return;
    if (1 + arg.size() <= command_buffer.available()) {
      command_buffer.append(" ");
      command_buffer.append(arg.begin(), arg.end());
    } else {
      ok = false;
    }
  });

  if (!ok) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_RUN_ASYNC_FAILED));
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  rpc::payload::ProcessRunAsync p = {};
  const size_t c_copy = etl::min(static_cast<size_t>(command_buffer.size()),
                                 sizeof(p.command) - 1U);
  if (c_copy > 0U) {
    etl::copy_n(command_buffer.begin(), c_copy, p.command);
  }

  const bool send_ok = Bridge.send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 0, p);
  if (!send_ok) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_RUN_ASYNC_FAILED));
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  if (handler.is_valid()) Process._pending_run_async.push({handler});
}

template <typename T>
void ProcessClass<T>::poll(int32_t pid,
                           typename ProcessClass<T>::ProcessPollHandler handler) {
  if (handler.is_valid() && _pending_polls.full()) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_LIMIT_REACHED));
    return;
  }

  rpc::payload::ProcessPoll p = {};
  p.pid = static_cast<uint32_t>(pid);

  if (!Bridge.send(rpc::CommandId::CMD_PROCESS_POLL, 0, p)) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::PROCESS_RUN_INTERNAL_ERROR));
    return;
  }

  if (handler.is_valid()) {
    _pending_polls.push({pid, handler});
  }
}

template <typename T>
void ProcessClass<T>::kill(int32_t pid) {
  rpc::payload::ProcessKill p = {};
  p.pid = static_cast<uint32_t>(pid);
  if (!Bridge.send(rpc::CommandId::CMD_PROCESS_KILL, 0, p)) {}
}

template <typename T>
void ProcessClass<T>::_onKillNotification(const rpc::payload::ProcessKill&) {
  // Linux notifies MCU that a process was killed. Clear local queues only —
  // do NOT re-send CMD_PROCESS_KILL (that would create an echo loop).
  reset();
}

template <typename T>
void ProcessClass<T>::_onRunAsyncResponse(
    const rpc::payload::ProcessRunAsyncResponse& msg) {
  if (_pending_run_async.empty()) return;
  const typename ProcessClass<T>::PendingRunAsync pending = _pending_run_async.front();
  _pending_run_async.pop();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<int32_t>(msg.pid));
  }
}

template <typename T>
void ProcessClass<T>::_onPollResponse(
    const rpc::payload::ProcessPollResponse& msg) {
  if (_pending_polls.empty()) return;
  const typename ProcessClass<T>::PendingPoll pending = _pending_polls.front();
  _pending_polls.pop();
  if (pending.handler.is_valid()) {
    pending.handler(
        static_cast<rpc::StatusCode>(msg.status), msg.exit_code,
        etl::span<const uint8_t>(msg.stdout_data.bytes, msg.stdout_data.size),
        etl::span<const uint8_t>(msg.stderr_data.bytes, msg.stderr_data.size));
  }
}

template <typename T>
void ProcessClass<T>::reset() {
  _pending_run_async.clear();
  _pending_polls.clear();
}

template class ProcessClass<void>;
ProcessType Process;

#endif
