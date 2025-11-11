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

#include "rpc_protocol.h"

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
  size_t key_len = strlen(key);
  size_t value_len = strlen(value);
  // Key + '\0' + Value
  size_t payload_len = key_len + 1 + value_len;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) return; // Evitar buffer overflow

  uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  memcpy(payload, key, key_len);
  payload[key_len] = '\0'; // Separador
  memcpy(payload + key_len + 1, value, value_len);

  Bridge.sendFrame(CMD_DATASTORE_PUT, payload, payload_len);
}

void DataStoreClass::requestGet(const char* key) {
  Bridge.sendFrame(CMD_DATASTORE_GET, (const uint8_t*)key, strlen(key));
}

// =================================================================================
// MailboxClass
// =================================================================================

MailboxClass::MailboxClass() {}

void MailboxClass::send(const char* message) {
  // NOTE: PROTOCOL.md describes CMD_MAILBOX_PROCESSED (0x41) as MCU -> Linux
  // indicating a message *received and processed* by the MCU. If the intent
  // is for the MCU to *send* a new message to Linux, a different command ID
  // and protocol definition would be required. Currently, this sends a placeholder
  // 'processed' message.
  uint16_t message_id = 0; // Placeholder
  uint8_t payload[2];
  rpc::write_u16_be(payload, message_id);
  Bridge.sendFrame(CMD_MAILBOX_PROCESSED, payload, 2);
}

void MailboxClass::send(const uint8_t* data, size_t length) {
  // NOTE: Same as above. This sends a placeholder 'processed' message.
  uint16_t message_id = 0; // Placeholder
  uint8_t payload[2];
  rpc::write_u16_be(payload, message_id);
  Bridge.sendFrame(CMD_MAILBOX_PROCESSED, payload, 2);
}

void MailboxClass::requestRead() {
  // Solicita a Linux que envíe el siguiente mensaje disponible.
  Bridge.sendFrame(CMD_MAILBOX_READ, nullptr, 0);
}

// ANÁLISIS: Eliminados available() y read() que no forman parte de la API V2 asíncrona.

// =================================================================================
// FileSystemClass
// =================================================================================

void FileSystemClass::write(const char* filePath, const uint8_t* data,
                            size_t length) {
  if (filePath == nullptr || strlen(filePath) == 0) return; // Validar path

  size_t filePath_len = strlen(filePath);
  // FilePath + '\0' + Data
  size_t payload_len = filePath_len + 1 + length;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) return; // Evitar buffer overflow

  uint8_t payload[rpc::MAX_PAYLOAD_SIZE];

  memcpy(payload, filePath, filePath_len);
  payload[filePath_len] = '\0'; // Separador
  memcpy(payload + filePath_len + 1, data, length);

  Bridge.sendFrame(CMD_FILE_WRITE, payload, payload_len);
}

void FileSystemClass::remove(const char* filePath) {
  if (filePath == nullptr || strlen(filePath) == 0) return; // Validar path
  Bridge.sendFrame(CMD_FILE_REMOVE, (const uint8_t*)filePath,
                   strlen(filePath));
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
      _digital_read_handler(nullptr),
      _analog_read_handler(nullptr),
      _process_run_handler(nullptr),
      _process_poll_handler(nullptr),
      _process_run_async_handler(nullptr),
      _file_system_read_handler(nullptr),
      _get_free_memory_handler(nullptr),
      _status_handler(nullptr) {}

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
        if (_datastore_get_handler && frame.header.payload_length > 1) {
          // Asume formato "key\0value"
          const char* key = (const char*)frame.payload;
          size_t key_len = strlen(key);
          if (key_len < frame.header.payload_length - 1) {
             const char* value = key + key_len + 1;
             _datastore_get_handler(key, value);
          }
        }
        break;

      case CMD_MAILBOX_READ_RESP:
        if (_mailbox_handler) {
          // Pasa el buffer y su longitud al callback del usuario
          _mailbox_handler(frame.payload, frame.header.payload_length);
        }
        break;

       case CMD_MAILBOX_AVAILABLE_RESP:
         // Este caso podría manejarse si el sketch necesita saber cuántos
         // mensajes hay pendientes en Linux, aunque no es lo habitual.
         // Podríamos añadir un callback si fuese necesario.
         // Por ahora, lo ignoramos ya que el flujo normal es requestRead -> onMailboxMessage.
         break;

      case CMD_PROCESS_RUN_RESP:
        if (_process_run_handler) {
           // Asume que el payload es una cadena C (terminada en null si viene de C)
           // o simplemente bytes si viene de Python. Se pasa como const char*.
          _process_run_handler((const char*)frame.payload);
        }
        break;

      case CMD_PROCESS_POLL_RESP:
        if (_process_poll_handler && frame.header.payload_length >= 6) { // Min payload: status(1) + exit_code(1) + stdout_len(2) + stderr_len(2)
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
            _process_poll_handler(status, exit_code, stdout_data, stdout_len, stderr_data, stderr_len);
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
        if (_file_system_read_handler && frame.header.payload_length > 0) {
           // Pass the payload as uint8_t* and its length
          _file_system_read_handler(frame.payload, frame.header.payload_length);
        }
        break;

      case CMD_GET_FREE_MEMORY_RESP:
        if (_get_free_memory_handler && frame.header.payload_length == 2) {
          uint16_t free_mem = (uint16_t)((frame.payload[0] << 8) | frame.payload[1]); // Big Endian
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
        // TODO: Implement platform-specific free memory retrieval here.
        // For AVR: (requires extern int __heap_start, *__brkval;)
        // int freeRam() {
        //   int v;
        //   return (int) &v - (__brkval == 0 ? (int) &__heap_start : (int) __brkval);
        // }
        // For SAMD/ESP32: use ESP.getFreeHeap() or similar.
        uint16_t free_mem = 0; // Placeholder, implement actual retrieval

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
        // Enviar respuesta inmediatamente: solo el valor (1 byte)
        uint8_t resp_payload[1] = {(uint8_t)(value & 0xFF)};
        sendFrame(CMD_DIGITAL_READ_RESP, resp_payload, 1);
        command_processed_internally = true;
        // Los comandos READ no suelen requerir ACK adicional a la respuesta
      }
      break;
    case CMD_ANALOG_READ:
      if (frame.header.payload_length == 1) {
        uint8_t pin = frame.payload[0];
        int value = ::analogRead(pin);
         // Enviar respuesta inmediatamente: solo el valor (2 bytes Big Endian)
        uint8_t resp_payload[2];
        rpc::write_u16_be(resp_payload, value);
        sendFrame(CMD_ANALOG_READ_RESP, resp_payload, 2);
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
           { // Linux solicita saber cuántos mensajes tiene el MCU para Linux
             // Actualmente, el MCU no mantiene una cola de mensajes para Linux, así que responde 0.
             uint8_t count_payload = 0; // u8 byte
             sendFrame(CMD_MAILBOX_AVAILABLE_RESP, &count_payload, 1);
             command_processed_internally = true;
           }
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
  sendFrame(CMD_PROCESS_RUN, (const uint8_t*)command, strlen(command));
}

void BridgeClass::requestProcessRunAsync(const char* command) {
  sendFrame(CMD_PROCESS_RUN_ASYNC, (const uint8_t*)command, strlen(command));
}

void BridgeClass::requestProcessPoll(int pid) {
  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, (uint16_t)pid);
  sendFrame(CMD_PROCESS_POLL, pid_payload, 2);
}

void BridgeClass::requestFileSystemRead(const char* filePath) {
  sendFrame(CMD_FILE_READ, (const uint8_t*)filePath, strlen(filePath));
}

void BridgeClass::requestGetFreeMemory() {
  sendFrame(CMD_GET_FREE_MEMORY, nullptr, 0);
}
