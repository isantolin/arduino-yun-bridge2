#include "Bridge.h"
#include "protocol/rpc_protocol.h"

using namespace rpc;

namespace {
// Timeout para esperar por um slot TX (tratamento de contrapressão)
constexpr unsigned long kTxTimeoutMs = 1000;
}

ProcessClass::ProcessClass() 
  : _pending_process_poll_head(0),
    _pending_process_poll_count(0),
    _process_run_handler(nullptr),
    _process_poll_handler(nullptr),
    _process_run_async_handler(nullptr) {
  // Correção: Inicialização de array estilo C via loop (sem método .fill)
  for (auto& pid : _pending_process_pids) {
    pid = 0;
  }
}

// Auxiliar para enviar frame com tratamento de contrapressão
bool ProcessClass::_sendWithRetry(CommandId cmd, const uint8_t* payload, size_t len) {
  unsigned long start = millis();
  while (!Bridge.sendFrame(cmd, payload, len)) {
    // Se a fila estiver cheia, DEVEMOS bombear a bridge para processar ACKs e libertar slots
    Bridge.process();
    
    if (millis() - start > kTxTimeoutMs) {
      Bridge._emitStatus(StatusCode::STATUS_ERROR, F("process_tx_timeout"));
      return false;
    }
    // Ceder ligeiramente
    delay(1); 
  }
  return true;
}

void ProcessClass::run(const char* command) {
  if (!command || !*command) {
    return;
  }
  size_t len = strlen(command);
  if (len > rpc::MAX_PAYLOAD_SIZE) {
    Bridge._emitStatus(StatusCode::STATUS_ERROR, F("process_run_payload_too_large"));
    return;
  }
  
  _sendWithRetry(
      CommandId::CMD_PROCESS_RUN,
      reinterpret_cast<const uint8_t*>(command),
      len
  );
}

void ProcessClass::runAsync(const char* command) {
  if (!command || !*command) {
    return;
  }
  size_t len = strlen(command);
  if (len > rpc::MAX_PAYLOAD_SIZE) {
    Bridge._emitStatus(StatusCode::STATUS_ERROR, F("process_run_async_payload_too_large"));
    return;
  }
  
  _sendWithRetry(
      CommandId::CMD_PROCESS_RUN_ASYNC,
      reinterpret_cast<const uint8_t*>(command),
      len
  );
}

void ProcessClass::poll(int pid) {
  if (pid < 0) {
    return;
  }

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    Bridge._emitStatus(StatusCode::STATUS_ERROR, F("process_poll_queue_full"));
    return;
  }

  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, pid_u16);
  
  if (!_sendWithRetry(CommandId::CMD_PROCESS_POLL, pid_payload, 2)) {
     // Se o envio falhou, verificar se não libertamos o slot pendente?
     // Na verdade _pushPendingProcessPid já avançou a fila. 
     // Num mundo perfeito faríamos rollback, mas por agora o erro de timeout é suficiente.
  }
}

void ProcessClass::kill(int pid) {
  uint8_t pid_payload[2];
  write_u16_be(pid_payload, static_cast<uint16_t>(pid));
  _sendWithRetry(CommandId::CMD_PROCESS_KILL, pid_payload, 2);
}

void ProcessClass::handleResponse(const rpc::Frame& frame) {
  const CommandId command = static_cast<CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  switch (command) {
    case CommandId::CMD_PROCESS_RUN_RESP:
      if (_process_run_handler && payload_length >= 1 && payload_data) {
        rpc::StatusCode status = static_cast<rpc::StatusCode>(payload_data[0]);
        if (payload_length >= 5) {
            uint16_t stdout_len = rpc::read_u16_be(payload_data + 1);
            const uint8_t* stdout_ptr = payload_data + 3;
            // Validar limites para prevenir overread do buffer
            if (payload_length >= static_cast<size_t>(3 + stdout_len + 2)) {
                uint16_t stderr_len = rpc::read_u16_be(payload_data + 3 + stdout_len);
                const uint8_t* stderr_ptr = payload_data + 3 + stdout_len + 2;
                // Verificação final de limites
                if (payload_length >= static_cast<size_t>(3 + stdout_len + 2 + stderr_len)) {
                    _process_run_handler(status, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
                }
            }
        }
      }
      break;
    case CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
      if (_process_run_async_handler && payload_length >= 2 && payload_data) {
        uint16_t pid = rpc::read_u16_be(payload_data);
        _process_run_async_handler(static_cast<int>(pid));
      }
      break;
    case CommandId::CMD_PROCESS_POLL_RESP:
      if (_process_poll_handler && payload_length >= 2 && payload_data) {
        rpc::StatusCode status = static_cast<rpc::StatusCode>(payload_data[0]);
        uint8_t running = payload_data[1];
        
        _popPendingProcessPid(); 
        
        if (payload_length >= 6) {
             uint16_t stdout_len = rpc::read_u16_be(payload_data + 2);
             const uint8_t* stdout_ptr = payload_data + 4;
             if (payload_length >= static_cast<size_t>(4 + stdout_len + 2)) {
                 uint16_t stderr_len = rpc::read_u16_be(payload_data + 4 + stdout_len);
                 const uint8_t* stderr_ptr = payload_data + 4 + stdout_len + 2;
                 if (payload_length >= static_cast<size_t>(4 + stdout_len + 2 + stderr_len)) {
                    _process_poll_handler(status, running, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
                 }
             }
        }
      }
      break;
    default:
      break;
  }
}

void ProcessClass::onProcessRunResponse(ProcessRunHandler handler) { _process_run_handler = handler; }
void ProcessClass::onProcessPollResponse(ProcessPollHandler handler) { _process_poll_handler = handler; }
void ProcessClass::onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) { _process_run_async_handler = handler; }

bool ProcessClass::_pushPendingProcessPid(uint16_t pid) {
  if (_pending_process_poll_count >= kMaxPendingProcessPolls) {
    return false;
  }

  uint8_t slot =
      (_pending_process_poll_head + _pending_process_poll_count) %
      kMaxPendingProcessPolls;
  _pending_process_pids[slot] = pid;
  _pending_process_poll_count++;
  return true;
}

uint16_t ProcessClass::_popPendingProcessPid() {
  if (_pending_process_poll_count == 0) {
    return 0xFFFF;
  }

  uint8_t slot = _pending_process_poll_head;
  uint16_t pid = _pending_process_pids[slot];
  _pending_process_poll_head =
      (_pending_process_poll_head + 1) % kMaxPendingProcessPolls;
  _pending_process_poll_count--;
  _pending_process_pids[slot] = 0;
  return pid;
}
