#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "Bridge.h"

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_constants.h"
#include "test_support.h"

// Mocks y Stubs Globales
HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

namespace {

void setup_test_env(TxCaptureStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);

  // Manually force sync state to enable command processing
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setIdle();
  Console.begin();
}

// --- TEST: SISTEMA Y GPIO (BRIDGE.CPP) ---
void test_extreme_bridge_commands() {
  TxCaptureStream stream;
  setup_test_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  // 1. Comando de Sistema Desconocido (Fuera de rango)
  f.header.command_id = 0x4F;  // Justo en el límite superior
  f.header.payload_length = 0;
  ba.dispatch(f);

  // 2. Comandos GPIO con payload inválido (null)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.header.payload_length = 0;  // Debería fallar silenciosamente o con error
  ba.dispatch(f);

  // 3. Comando GET_CAPABILITIES (Cubre ramas de features)
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
  f.header.payload_length = 0;
  ba.dispatch(f);

  // 4. SET_BAUDRATE con payload malformado (longitud != 4)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE);
  f.header.payload_length = 2;
  ba.dispatch(f);
}

// --- TEST: DATASTORE LÍMITES (DATASTORE.CPP) ---
void test_extreme_datastore() {
  TxCaptureStream stream;
  setup_test_env(stream);

  // 1. Put con Key/Value nulos
  DataStore.put(nullptr, "val");
  DataStore.put("key", nullptr);

  // 2. Put con Key/Value excediendo límites
  char long_key[rpc::RPC_MAX_DATASTORE_KEY_LENGTH + 10];
  etl::fill_n(long_key, sizeof(long_key), 'k');
  long_key[sizeof(long_key) - 1] = '\0';
  DataStore.put(long_key, "val");

  // 3. Response con longitud de valor inconsistente
  rpc::Frame resp;
  resp.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
  resp.header.payload_length = 5;
  resp.payload[0] = 10;  // Dice que vienen 10 bytes, pero solo hay 4 restantes
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.dispatch(resp);
}

// --- TEST: FILESYSTEM ERRORES (FILESYSTEM.CPP) ---
void test_extreme_filesystem() {
  TxCaptureStream stream;
  setup_test_env(stream);

  // 1. Write con path nulo o data nula
  FileSystem.write(nullptr, etl::span<const uint8_t>());
  FileSystem.write("/t", etl::span<const uint8_t>());

  // 2. Write con path extremadamente largo
  char long_path[200];
  etl::fill_n(long_path, sizeof(long_path), 'a');
  long_path[sizeof(long_path) - 1] = '\0';
  FileSystem.write(long_path, etl::span<const uint8_t>((const uint8_t*)"d", 1));

  // 3. Response malformada (sin payload)
  rpc::Frame resp;
  resp.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
  resp.header.payload_length = 0;
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.dispatch(resp);
}

// --- TEST: PROCESS Y MAILBOX (PROCESS.CPP / MAILBOX.CPP) ---
void test_extreme_process_mailbox() {
  TxCaptureStream stream;
  setup_test_env(stream);

  // 1. Mailbox: Send con data nula
  Mailbox.send(etl::span<const uint8_t>());

  // 2. Process: RunAsync con comando nulo o largo
  Process.runAsync(nullptr);

  // 3. Process: Poll con PID inválido
  Process.poll(-1);
}

}  // namespace

int main() {
  printf("EXTREME ARDUINO COVERAGE TEST START\n");
  test_extreme_bridge_commands();
  test_extreme_datastore();
  test_extreme_filesystem();
  test_extreme_process_mailbox();
  printf("EXTREME ARDUINO COVERAGE TEST END\n");
  return 0;
}

Stream* g_arduino_stream_delegate = nullptr;
