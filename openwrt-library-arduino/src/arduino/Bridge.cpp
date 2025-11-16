/*
 * This file is part of Arduino Yun Ecosystem v2.
 *
 * Copyright (C) 2025 Ignacio Santolin and contributors
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */
#include "Bridge.h"

#include <string.h> // Para strcmp, strlen, memcpy
#include <stdlib.h> // Para atoi
#include <stdint.h>

#include "protocol/rpc_protocol.h"

#define BRIDGE_BAUDRATE 115200

using namespace rpc;

// =================================================================================
// Global Instances
// =================================================================================

BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

#if BRIDGE_DEBUG_IO
static void bridge_debug_log_gpio(const char* action, uint8_t pin, int value) {
  if (!Console) return;
  Console.print(F("[GPIO] "));
  Console.print(action);
  Console.print(F(" D"));
  Console.print(pin);
  Console.print(F(" = "));
  Console.println(value);
}
#endif

namespace {

#if defined(ARDUINO_ARCH_AVR)
extern char __heap_start;
extern char* __brkval;

uint16_t calculateFreeMemoryBytes() {
  char stack_top;
  char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) {
    free_bytes = 0;
  }
  if (free_bytes > 0xFFFF) {
    free_bytes = 0xFFFF;
  }
  return static_cast<uint16_t>(free_bytes);
}
#else
uint16_t calculateFreeMemoryBytes() {
  // Platform not yet supported; report 0 to signal unavailable data.
  return 0;
}
#endif

}  // namespace

// =================================================================================
// ConsoleClass
// =================================================================================

ConsoleClass::ConsoleClass()
    : _begun(false),
      _rx_buffer_head(0),
      _rx_buffer_tail(0),
      _xoff_sent(false) {}

void ConsoleClass::begin() {
  _begun = true;
  _rx_buffer_head = 0;
  _rx_buffer_tail = 0;
  _xoff_sent = false;
}

size_t ConsoleClass::write(uint8_t c) { return write(&c, 1); }

size_t ConsoleClass::write(const uint8_t* buffer, size_t size) {
  if (!_begun) return 0;
  // Limitar el tamaño del payload para evitar fragmentación excesiva
  size_t remaining = size;
  size_t offset = 0;
  while (remaining > 0) {
      size_t chunk_size = remaining > MAX_PAYLOAD_SIZE ? MAX_PAYLOAD_SIZE : remaining;
      Bridge.sendFrame(CMD_CONSOLE_WRITE, buffer + offset, chunk_size);
      offset += chunk_size;
      remaining -= chunk_size;
      // Añadir un pequeño delay puede ayudar si hay problemas de buffer
      // delayMicroseconds(100);
  }
  return size;
}

int ConsoleClass::available() {
  return (_rx_buffer_head - _rx_buffer_tail + CONSOLE_RX_BUFFER_SIZE) %
         CONSOLE_RX_BUFFER_SIZE;
}

int ConsoleClass::peek() {
  if (_rx_buffer_head == _rx_buffer_tail) return -1;
  return _rx_buffer[_rx_buffer_tail];
}

int ConsoleClass::read() {
  if (_rx_buffer_head == _rx_buffer_tail) return -1;
  uint8_t c = _rx_buffer[_rx_buffer_tail];
  _rx_buffer_tail = (_rx_buffer_tail + 1) % CONSOLE_RX_BUFFER_SIZE;

  // Enviar XON si el buffer baja del límite inferior
  if (_xoff_sent && available() < CONSOLE_BUFFER_LOW_WATER) {
    Bridge.sendFrame(CMD_XON, nullptr, 0);
    _xoff_sent = false;
  }

  return c;
}

void ConsoleClass::flush() {
    // Para HardwareSerial, flush() espera a que se complete la transmisión saliente.
    // No hay buffer de recepción que limpiar explícitamente aquí.
    Serial1.flush();
}

void ConsoleClass::_push(const uint8_t* buffer, size_t size) {
  for (size_t i = 0; i < size; i++) {
    uint16_t next_head = (_rx_buffer_head + 1) % CONSOLE_RX_BUFFER_SIZE;
    if (next_head != _rx_buffer_tail) {
      _rx_buffer[_rx_buffer_head] = buffer[i];
      _rx_buffer_head = next_head;
    } else {
      // Buffer lleno, descartar byte. Podríamos loggear esto si tuviéramos un log.
    }
  }

  // Enviar XOFF si el buffer supera el límite superior
  if (!_xoff_sent && available() > CONSOLE_BUFFER_HIGH_WATER) {
    Bridge.sendFrame(CMD_XOFF, nullptr, 0);
    _xoff_sent = true;
  }
}

// =================================================================================
// DataStoreClass
// =================================================================================

DataStoreClass::DataStoreClass() {}

void DataStoreClass::put(const char* key, const char* value) {
  if (!key || !value) return;

  size_t key_len = strlen(key);
  size_t value_len = strlen(value);
  if (key_len == 0 || key_len > 255 || value_len > 255) return;

  const size_t payload_len = 2 + key_len + value_len;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) return;

  uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);
  payload[1 + key_len] = static_cast<uint8_t>(value_len);
  memcpy(payload + 2 + key_len, value, value_len);

  Bridge.sendFrame(CMD_DATASTORE_PUT, payload, static_cast<uint16_t>(payload_len));
}

void DataStoreClass::requestGet(const char* key) {
  if (!key) return;
  size_t key_len = strlen(key);
  if (key_len == 0 || key_len > 255) return;

  uint8_t payload[1 + 255];
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);

  Bridge._trackPendingDatastoreKey(key);
  Bridge.sendFrame(CMD_DATASTORE_GET, payload, static_cast<uint16_t>(key_len + 1));
}

// =================================================================================
// MailboxClass
// =================================================================================

MailboxClass::MailboxClass() {}

void MailboxClass::send(const char* message) {
  if (!message) return;
  send(reinterpret_cast<const uint8_t*>(message), strlen(message));
}

void MailboxClass::send(const uint8_t* data, size_t length) {
  if (!data || length == 0) return;

  size_t max_payload = rpc::MAX_PAYLOAD_SIZE - 2;
  if (length > max_payload) {
    length = max_payload;
  }

  uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  rpc::write_u16_be(payload, static_cast<uint16_t>(length));
  memcpy(payload + 2, data, length);
  Bridge.sendFrame(CMD_MAILBOX_PUSH, payload, static_cast<uint16_t>(length + 2));
}

void MailboxClass::requestRead() {
  // Solicita a Linux que envíe el siguiente mensaje disponible.
  Bridge.sendFrame(CMD_MAILBOX_READ, nullptr, 0);
}

void MailboxClass::requestAvailable() {
  // Solicita a Linux la cantidad de mensajes pendientes para el MCU.
  Bridge.sendFrame(CMD_MAILBOX_AVAILABLE, nullptr, 0);
}

// ANÁLISIS: Eliminados available() y read() que no forman parte de la API V2 asíncrona.

// =================================================================================
// FileSystemClass
// =================================================================================

void FileSystemClass::write(const char* filePath, const uint8_t* data,
                            size_t length) {
  if (!filePath || !data) return;
  size_t path_len = strlen(filePath);
  if (path_len == 0 || path_len > 255) return;

  const size_t max_data = rpc::MAX_PAYLOAD_SIZE - 3 - path_len;
  if (length > max_data) {
    length = max_data;
  }

  uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  rpc::write_u16_be(payload + 1 + path_len, static_cast<uint16_t>(length));
  if (length > 0) {
    memcpy(payload + 3 + path_len, data, length);
  }

  Bridge.sendFrame(CMD_FILE_WRITE, payload,
                   static_cast<uint16_t>(path_len + length + 3));
}

void FileSystemClass::remove(const char* filePath) {
  if (!filePath) return;
  size_t path_len = strlen(filePath);
  if (path_len == 0 || path_len > 255) return;

  uint8_t payload[1 + 255];
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  Bridge.sendFrame(CMD_FILE_REMOVE, payload,
                   static_cast<uint16_t>(path_len + 1));
}

// =================================================================================
// ProcessClass
// =================================================================================

ProcessClass::ProcessClass() {}

void ProcessClass::kill(int pid) {
  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, (uint16_t)pid);
  Bridge.sendFrame(CMD_PROCESS_KILL, pid_payload, 2);
}

// =================================================================================
// BridgeClass
// =================================================================================

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream),
      _parser(),
      _builder(),
      _command_handler(nullptr),
      _datastore_get_handler(nullptr),
      _mailbox_handler(nullptr),
      _mailbox_available_handler(nullptr),
      _digital_read_handler(nullptr),
      _analog_read_handler(nullptr),
      _process_run_handler(nullptr),
      _process_poll_handler(nullptr),
      _process_run_async_handler(nullptr),
      _file_system_read_handler(nullptr),
      _get_free_memory_handler(nullptr),
      _status_handler(nullptr),
      _pending_datastore_head(0),
      _pending_datastore_count(0),
      _pending_process_poll_head(0),
      _pending_process_poll_count(0) {
  for (uint8_t i = 0; i < kMaxPendingDatastore; ++i) {
    _pending_datastore_keys[i] = nullptr;
  }
  for (uint8_t i = 0; i < kMaxPendingProcessPolls; ++i) {
    _pending_process_pids[i] = 0;
  }
}

void BridgeClass::_trackPendingDatastoreKey(const char* key) {
  if (!key || !*key) {
    return;
  }
  if (_pending_datastore_count == kMaxPendingDatastore) {
    _pending_datastore_head = (_pending_datastore_head + 1) % kMaxPendingDatastore;
    _pending_datastore_count--;
  }
  uint8_t index = (_pending_datastore_head + _pending_datastore_count) % kMaxPendingDatastore;
  _pending_datastore_keys[index] = key;
  _pending_datastore_count++;
}

const char* BridgeClass::_popPendingDatastoreKey() {
  if (_pending_datastore_count == 0) {
    return nullptr;
  }
  const char* key = _pending_datastore_keys[_pending_datastore_head];
  _pending_datastore_head = (_pending_datastore_head + 1) % kMaxPendingDatastore;
  _pending_datastore_count--;
  return key;
}

bool BridgeClass::_pushPendingProcessPid(uint16_t pid) {
  if (_pending_process_poll_count == kMaxPendingProcessPolls) {
    return false;
  }
  uint8_t index = (_pending_process_poll_head + _pending_process_poll_count) % kMaxPendingProcessPolls;
  _pending_process_pids[index] = pid;
  _pending_process_poll_count++;
  return true;
}

uint16_t BridgeClass::_popPendingProcessPid() {
  if (_pending_process_poll_count == 0) {
    return 0xFFFF;
  }
  uint16_t pid = _pending_process_pids[_pending_process_poll_head];
  _pending_process_poll_head = (_pending_process_poll_head + 1) % kMaxPendingProcessPolls;
  _pending_process_poll_count--;
  return pid;
}

void BridgeClass::begin() {
  // CORRECCIÓN: Usar static_cast en lugar de dynamic_cast porque RTTI está desactivado.
  // Es seguro aquí porque sabemos que Bridge siempre se instancia con Serial1 (HardwareSerial).
  HardwareSerial* hwSerial = static_cast<HardwareSerial*>(&_stream);
  if (hwSerial) {
    hwSerial->begin(BRIDGE_BAUDRATE);
  }
  // Añadir un pequeño delay o flush para asegurar que el puerto serie esté listo
  delay(10);
  _stream.flush(); // Asegura que cualquier dato pendiente en el buffer TX se envíe
  _parser.reset();
  Console.begin(); // Inicializa la instancia global de Console
}

// --- Register Callbacks ---
void BridgeClass::onMailboxMessage(MailboxHandler handler) { _mailbox_handler = handler; }
void BridgeClass::onMailboxAvailableResponse(MailboxAvailableHandler handler) {
  _mailbox_available_handler = handler;
}
void BridgeClass::onCommand(CommandHandler handler) { _command_handler = handler; }
void BridgeClass::onDataStoreGetResponse(DataStoreGetHandler handler) { _datastore_get_handler = handler; }
void BridgeClass::onDigitalReadResponse(DigitalReadHandler handler) { _digital_read_handler = handler; }
void BridgeClass::onAnalogReadResponse(AnalogReadHandler handler) { _analog_read_handler = handler; }
void BridgeClass::onProcessRunResponse(ProcessRunHandler handler) { _process_run_handler = handler; }
void BridgeClass::onProcessPollResponse(ProcessPollHandler handler) { _process_poll_handler = handler; }
void BridgeClass::onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) { _process_run_async_handler = handler; }
void BridgeClass::onFileSystemReadResponse(FileSystemReadHandler handler) { _file_system_read_handler = handler; }
void BridgeClass::onGetFreeMemoryResponse(GetFreeMemoryHandler handler) { _get_free_memory_handler = handler; }
void BridgeClass::onStatus(StatusHandler handler) { _status_handler = handler; }


/**
 * @brief Procesa los datos entrantes del stream serial.
 * Debe llamarse repetidamente en el loop principal del sketch.
 */
void BridgeClass::process() {
  while (_stream.available()) {
    uint8_t byte = _stream.read();
    rpc::Frame frame;
    // consume() decodifica COBS, verifica CRC y parsea el header/payload
    if (_parser.consume(byte, frame)) {
      dispatch(frame); // Si se recibe una trama válida, se procesa
    }
    // Si consume devuelve false, o no era fin de paquete (0x00) o hubo error
  }
}

/**
 * @brief Enruta una trama RPC válida a su manejador correspondiente.
 * @param frame La trama RPC recibida y validada.
 */
void BridgeClass::dispatch(const rpc::Frame& frame) {
  // --- Manejo de Respuestas Asíncronas (Callback Flow) ---
  // Las respuestas tienen IDs >= 0x80
  if (frame.header.command_id >= 0x80) {
    switch (frame.header.command_id) {
      case CMD_DIGITAL_READ_RESP:
        if (_digital_read_handler && frame.header.payload_length == 1) {
          int value = frame.payload[0];
          // The pin is not part of the response payload. The handler should be aware of the context.
          _digital_read_handler(value); // Pass only the value
        }
        break;

      case CMD_ANALOG_READ_RESP:
          if (_analog_read_handler && frame.header.payload_length == 2) {
              // Asume Big Endian para el valor (2 bytes)
              int value = (int)rpc::read_u16_be(frame.payload);
              // The pin is not part of the response payload. The handler should be aware of the context.
              _analog_read_handler(value); // Pass only the value
          }
          break;

      case CMD_DATASTORE_GET_RESP:
        if (_datastore_get_handler && frame.header.payload_length >= 1) {
          uint8_t value_len = frame.payload[0];
          if (frame.header.payload_length >= static_cast<uint16_t>(1 + value_len)) {
            const uint8_t* value_ptr = frame.payload + 1;
            const char* key = _popPendingDatastoreKey();
            _datastore_get_handler(key, value_ptr, value_len);
          }
        }
        break;

      case CMD_MAILBOX_READ_RESP:
        if (_mailbox_handler && frame.header.payload_length >= 2) {
          uint16_t message_len = rpc::read_u16_be(frame.payload);
          if (frame.header.payload_length >= static_cast<uint16_t>(2 + message_len)) {
            _mailbox_handler(frame.payload + 2, message_len);
          }
        }
        break;

       case CMD_MAILBOX_AVAILABLE_RESP:
        if (_mailbox_available_handler && frame.header.payload_length == 1) {
          uint8_t count = frame.payload[0];
          _mailbox_available_handler(count);
        }
         break;

      case CMD_PROCESS_RUN_RESP:
        if (_process_run_handler && frame.header.payload_length >= 5) {
          const uint8_t* cursor = frame.payload;
          uint8_t status = *cursor++;
          uint16_t stdout_len = rpc::read_u16_be(cursor);
          cursor += 2;
          if (frame.header.payload_length < static_cast<uint16_t>(5 + stdout_len)) {
            break;
          }
          const uint8_t* stdout_ptr = cursor;
          cursor += stdout_len;
          uint16_t stderr_len = rpc::read_u16_be(cursor);
          cursor += 2;
          if (frame.header.payload_length < static_cast<uint16_t>(5 + stdout_len + stderr_len)) {
            break;
          }
          const uint8_t* stderr_ptr = cursor;
          _process_run_handler(status, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
        }
        break;

      case CMD_PROCESS_POLL_RESP:
        if (frame.header.payload_length >= 6) { // Min payload: status(1) + exit_code(1) + stdout_len(2) + stderr_len(2)
          uint16_t pid = _popPendingProcessPid();
          const uint8_t* p = frame.payload;
          uint8_t status = *p++;
          uint8_t exit_code = *p++;
          uint16_t stdout_len = rpc::read_u16_be(p);
          p += 2;
          uint16_t stderr_len = rpc::read_u16_be(p);
          p += 2;

          if (frame.header.payload_length >= (6 + stdout_len + stderr_len)) {
            const uint8_t* stdout_data = p;
            const uint8_t* stderr_data = p + stdout_len;
            if (_process_poll_handler) {
              _process_poll_handler(status, exit_code, stdout_data, stdout_len, stderr_data, stderr_len);
            }

            if (pid != 0xFFFF && status == STATUS_OK && (stdout_len > 0 || stderr_len > 0)) {
              // Solicita automáticamente el siguiente fragmento cuando se recibe datos parciales.
              requestProcessPoll((int)pid);
            }
          } else {
            // Log error for malformed payload
          }
        } else {
          // Log error for malformed payload
        }
        break;

      case CMD_PROCESS_RUN_ASYNC_RESP:
        if (_process_run_async_handler && frame.header.payload_length == 2) {
          uint16_t pid = rpc::read_u16_be(frame.payload);
          _process_run_async_handler(pid);
        }
        break;

      case CMD_FILE_READ_RESP:
        if (_file_system_read_handler && frame.header.payload_length >= 2) {
          uint16_t data_len = rpc::read_u16_be(frame.payload);
          if (frame.header.payload_length >= static_cast<uint16_t>(2 + data_len)) {
            _file_system_read_handler(frame.payload + 2, data_len);
          }
        }
        break;

      case CMD_GET_FREE_MEMORY_RESP:
        if (_get_free_memory_handler && frame.header.payload_length >= 2) {
          uint16_t free_mem = rpc::read_u16_be(frame.payload);
          _get_free_memory_handler(free_mem);
        }
        break;

      // Otros casos de respuesta...
      case STATUS_ACK:
      case STATUS_ERROR:
      case STATUS_CMD_UNKNOWN:
      case STATUS_MALFORMED:
      case STATUS_CRC_MISMATCH:
      case STATUS_TIMEOUT:
      case STATUS_NOT_IMPLEMENTED:
        if (_status_handler) {
          _status_handler((uint8_t)frame.header.command_id, frame.payload,
                          frame.header.payload_length);
        }
        break;

       default:
         // Respuesta desconocida o no manejada explícitamente.
         // Podría pasar al _command_handler general si quisiéramos.
         break;
    }
     // Si era una respuesta, ya ha sido manejada (o ignorada), terminamos aquí.
    return;
  }

  // --- Manejo de Comandos Entrantes (No Respuestas) ---
  // IDs < 0x80

  bool command_processed_internally = false;
  bool requires_ack = false;

  switch (frame.header.command_id) {
    case CMD_GET_VERSION:
      {
        uint8_t version_payload[2] = {
            (uint8_t)BRIDGE_FIRMWARE_VERSION_MAJOR,
            (uint8_t)BRIDGE_FIRMWARE_VERSION_MINOR};
        sendFrame(CMD_GET_VERSION_RESP, version_payload, sizeof(version_payload));
        command_processed_internally = true;
      }
      break;
    case STATUS_ACK:
    case STATUS_CMD_UNKNOWN:
    case STATUS_MALFORMED:
    case STATUS_CRC_MISMATCH:
    case STATUS_TIMEOUT:
    case STATUS_NOT_IMPLEMENTED:
      if (_status_handler) {
        _status_handler((uint8_t)frame.header.command_id, frame.payload,
                        frame.header.payload_length);
      }
      command_processed_internally = true;
      break;
    case CMD_GET_FREE_MEMORY:
      {
        uint16_t free_mem = calculateFreeMemoryBytes();

        uint8_t resp_payload[2];
        // Pack free_mem as Big Endian
        resp_payload[0] = (free_mem >> 8) & 0xFF;
        resp_payload[1] = free_mem & 0xFF;
        sendFrame(CMD_GET_FREE_MEMORY_RESP, resp_payload, 2);
        command_processed_internally = true;
      }
      break;

    // --- Comandos I/O que la librería maneja automáticamente ---
    case CMD_SET_PIN_MODE:
      if (frame.header.payload_length == 2) {
      uint8_t pin = frame.payload[0];
      uint8_t mode = frame.payload[1];
      ::pinMode(pin, mode);
    #if BRIDGE_DEBUG_IO
      bridge_debug_log_gpio("pinMode", pin, mode);
    #endif
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CMD_DIGITAL_WRITE:
      if (frame.header.payload_length == 2) {
      uint8_t pin = frame.payload[0];
      uint8_t value = frame.payload[1] ? HIGH : LOW;
      ::digitalWrite(pin, value);
    #if BRIDGE_DEBUG_IO
      bridge_debug_log_gpio("digitalWrite", pin, value == HIGH ? 1 : 0);
    #endif
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CMD_ANALOG_WRITE:
      if (frame.header.payload_length == 2) {
        // analogWrite espera int, pero el payload es uint8_t
        ::analogWrite(frame.payload[0], (int)frame.payload[1]);
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CMD_DIGITAL_READ:
      if (frame.header.payload_length == 1) {
        uint8_t pin = frame.payload[0];
        int value = ::digitalRead(pin);
#if BRIDGE_DEBUG_IO
        bridge_debug_log_gpio("digitalRead", pin, value);
#endif
        uint8_t resp_payload = static_cast<uint8_t>(value & 0xFF);
        sendFrame(CMD_DIGITAL_READ_RESP, &resp_payload, 1);
        command_processed_internally = true;
      }
      break;
    case CMD_ANALOG_READ:
      if (frame.header.payload_length == 1) {
        uint8_t pin = frame.payload[0];
        int value = ::analogRead(pin);
#if BRIDGE_DEBUG_IO
        bridge_debug_log_gpio("analogRead", pin, value);
#endif
        uint8_t resp_payload[2];
        rpc::write_u16_be(resp_payload,
                          static_cast<uint16_t>(value & 0xFFFF));
        sendFrame(CMD_ANALOG_READ_RESP, resp_payload, sizeof(resp_payload));
        command_processed_internally = true;
      }
      break;

    // --- Otros comandos que requieren ACK pero se manejan aquí o por el usuario ---
    case CMD_CONSOLE_WRITE:
      // El payload se pasa a la instancia Console
      Console._push(frame.payload, frame.header.payload_length);
      // No necesita ACK explícito aquí, XON/XOFF maneja el flujo.
      command_processed_internally = true; // Se considera manejado internamente por Console
      break;
    case CMD_DATASTORE_PUT: // Podría ser manejado por el usuario también
    case CMD_FILE_WRITE:
    case CMD_FILE_REMOVE:
    case CMD_PROCESS_KILL:
      requires_ack = true; // Estos necesitan ACK, pero la acción la puede hacer el usuario
      break; // Pasa al handler del usuario si existe

        case CMD_MAILBOX_AVAILABLE:
          // Este comando debería originarse en el MCU hacia Linux; si llega
          // desde Linux se delega al manejador de usuario.
          break;
    // Comandos que Linux envía pero Arduino no implementa directamente la acción
    case CMD_DATASTORE_GET:
    case CMD_MAILBOX_READ: // El sketch llama a Mailbox.requestRead() para iniciar esto
    case CMD_FILE_READ:
    case CMD_PROCESS_RUN:
    case CMD_PROCESS_RUN_ASYNC:
    case CMD_PROCESS_POLL:
      // Estos comandos son solicitudes a Linux, Arduino no los recibe.
      // Si llegaran, serían inesperados. Podríamos enviar error.
      // O simplemente pasarlos al handler del usuario.
       break; // Pasa al handler del usuario

    default:
      // Comando desconocido o no manejado internamente
      break;
  }

  // Enviar ACK si es necesario y fue procesado (interna o externamente)
  if (requires_ack) {
      sendFrame(STATUS_ACK, nullptr, 0);
  }

  // --- Llamar al Manejador de Comandos del Usuario ---
  // Si el comando no fue completamente manejado por la librería Y
  // si el usuario ha registrado un callback onCommand, se lo pasamos.
  if (!command_processed_internally && _command_handler) {
    _command_handler(frame);
  } else if (!command_processed_internally && !requires_ack) {
    // Si no fue manejado internamente, no requería ACK, y no hay handler de usuario,
    // podríamos enviar CMD_UNKNOWN o simplemente ignorarlo.
    // Ignorarlo es más simple por ahora.
  }
}

void BridgeClass::_emitStatus(uint8_t status_code, const char* message) {
  const uint8_t* payload = nullptr;
  uint16_t length = 0;
  if (message && *message) {
    length = static_cast<uint16_t>(strlen(message));
    if (length > rpc::MAX_PAYLOAD_SIZE) {
      length = rpc::MAX_PAYLOAD_SIZE;
    }
    payload = reinterpret_cast<const uint8_t*>(message);
  }
  sendFrame(status_code, payload, length);
  if (_status_handler) {
    _status_handler(status_code, payload, length);
  }
}


/**
 * @brief Construye y envía una trama RPC a través del stream serial.
 * @param command_id ID del comando o estado a enviar.
 * @param payload Puntero al buffer de datos del payload.
 * @param payload_len Longitud del payload en bytes.
 */
void BridgeClass::sendFrame(uint16_t command_id, const uint8_t* payload,
                            uint16_t payload_len) {
  uint8_t raw_frame_buf[rpc::MAX_RAW_FRAME_SIZE];

  // build() crea Header + Payload + CRC en raw_frame_buf
  size_t raw_len =
      _builder.build(raw_frame_buf, command_id, payload, payload_len);

  if (raw_len == 0) {
    // Error en la construcción (ej. payload demasiado grande)
    return;
  }

  uint8_t cobs_buf[rpc::COBS_BUFFER_SIZE];
  // encode() aplica COBS a la trama raw
  size_t cobs_len = cobs::encode(raw_frame_buf, raw_len, cobs_buf);

  // Envía la trama COBS seguida del terminador 0x00
  size_t written = _stream.write(cobs_buf, cobs_len);
  written += _stream.write((uint8_t)0x00);

  // Podríamos añadir verificación de 'written' si _stream.write devuelve algo útil
  // y manejar errores de escritura si es necesario.
}

// --- Public API Methods ---

// Note: These functions directly perform local hardware operations using the Arduino API.
// They do NOT send RPC commands to Linux. For sending RPC commands to Linux
// (e.g., to request a read from a pin), use the `requestXyz` methods.

void BridgeClass::pinMode(uint8_t pin, uint8_t mode) {
  ::pinMode(pin, mode);
}

void BridgeClass::digitalWrite(uint8_t pin, uint8_t value) {
  ::digitalWrite(pin, value);
}

void BridgeClass::analogWrite(uint8_t pin, int value) {
  // Asegurarse de que el valor está en el rango 0-255
  uint8_t val_u8 = constrain(value, 0, 255);
  ::analogWrite(pin, (int)val_u8);
}

void BridgeClass::requestDigitalRead(uint8_t pin) {
  uint8_t payload[1] = {pin};
  sendFrame(CMD_DIGITAL_READ, payload, 1); // Envía solicitud a Linux
}

void BridgeClass::requestAnalogRead(uint8_t pin) {
  uint8_t payload[1] = {pin};
  sendFrame(CMD_ANALOG_READ, payload, 1); // Envía solicitud a Linux
}

void BridgeClass::requestProcessRun(const char* command) {
  if (!command) {
    return;
  }
  size_t cmd_len = strlen(command);
  if (cmd_len == 0) {
    return;
  }
  if (cmd_len > rpc::MAX_PAYLOAD_SIZE) {
    _emitStatus(STATUS_ERROR, "process_run_payload_too_large");
    return;
  }
  sendFrame(
      CMD_PROCESS_RUN,
      reinterpret_cast<const uint8_t*>(command),
      static_cast<uint16_t>(cmd_len));
}

void BridgeClass::requestProcessRunAsync(const char* command) {
  if (!command) {
    return;
  }
  size_t cmd_len = strlen(command);
  if (cmd_len == 0) {
    return;
  }
  if (cmd_len > rpc::MAX_PAYLOAD_SIZE) {
    _emitStatus(STATUS_ERROR, "process_run_async_payload_too_large");
    return;
  }
  sendFrame(
      CMD_PROCESS_RUN_ASYNC,
      reinterpret_cast<const uint8_t*>(command),
      static_cast<uint16_t>(cmd_len));
}

void BridgeClass::requestProcessPoll(int pid) {
  if (pid < 0) {
    return;
  }

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    _emitStatus(STATUS_ERROR, "process_poll_queue_full");
    return;
  }

  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, pid_u16);
  sendFrame(CMD_PROCESS_POLL, pid_payload, 2);
}

void BridgeClass::requestFileSystemRead(const char* filePath) {
  if (!filePath) return;
  size_t path_len = strlen(filePath);
  if (path_len == 0 || path_len > 255) return;

  uint8_t payload[1 + 255];
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  sendFrame(CMD_FILE_READ, payload, static_cast<uint16_t>(path_len + 1));
}

void BridgeClass::requestGetFreeMemory() {
  sendFrame(CMD_GET_FREE_MEMORY, nullptr, 0);
}
