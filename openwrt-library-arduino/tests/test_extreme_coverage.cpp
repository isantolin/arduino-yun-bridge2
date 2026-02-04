#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

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

// Stream de captura para verificar salida del Bridge
class CaptureStream : public Stream {
public:
    ByteBuffer<4096> tx;
    size_t write(uint8_t c) override { tx.push(c); return 1; }
    size_t write(const uint8_t* b, size_t s) override { tx.append(b, s); return s; }
    int available() override { return 0; }
    int read() override { return -1; }
    int peek() override { return -1; }
    void flush() override {}
};

void setup_test_env(CaptureStream& stream) {
    Bridge.~BridgeClass();
    new (&Bridge) BridgeClass(stream);
    Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    
    // Manually force sync state to enable command processing
    Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete();
    Console.begin();
}

// --- TEST: SISTEMA Y GPIO (BRIDGE.CPP) ---
void test_extreme_bridge_commands() {
    CaptureStream stream;
    setup_test_env(stream);

    rpc::Frame f;
    // 1. Comando de Sistema Desconocido (Fuera de rango)
    f.header.command_id = 0x4F; // Justo en el límite superior
    f.header.payload_length = 0;
    Bridge.dispatch(f);

    // 2. Comandos GPIO con payload inválido (null)
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    f.header.payload_length = 0; // Debería fallar silenciosamente o con error
    Bridge.dispatch(f);

    // 3. Comando GET_CAPABILITIES (Cubre ramas de features)
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
    f.header.payload_length = 0;
    Bridge.dispatch(f);

    // 4. SET_BAUDRATE con payload malformado (longitud != 4)
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE);
    f.header.payload_length = 2;
    Bridge.dispatch(f);
}

// --- TEST: DATASTORE LÍMITES (DATASTORE.CPP) ---
void test_extreme_datastore() {
    CaptureStream stream;
    setup_test_env(stream);

    // 1. Put con Key/Value nulos
    DataStore.put(nullptr, "val");
    DataStore.put("key", nullptr);
    
    // 2. Put con Key/Value excediendo límites
    char long_key[rpc::RPC_MAX_DATASTORE_KEY_LENGTH + 10];
    memset(long_key, 'k', sizeof(long_key));
    long_key[sizeof(long_key)-1] = '\0';
    DataStore.put(long_key, "val");

    // 3. Response con longitud de valor inconsistente
    rpc::Frame resp;
    resp.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
    resp.header.payload_length = 5;
    resp.payload[0] = 10; // Dice que vienen 10 bytes, pero solo hay 4 restantes
    DataStore.handleResponse(resp);
}

// --- TEST: FILESYSTEM ERRORES (FILESYSTEM.CPP) ---
void test_extreme_filesystem() {
    CaptureStream stream;
    setup_test_env(stream);

    // 1. Write con path nulo o data nula
    FileSystem.write(nullptr, (const uint8_t*)"d", 1);
    FileSystem.write("/t", nullptr, 0);

    // 2. Write con path extremadamente largo
    char long_path[200];
    memset(long_path, 'a', sizeof(long_path));
    long_path[sizeof(long_path)-1] = '\0';
    FileSystem.write(long_path, (const uint8_t*)"d", 1);

    // 3. Response malformada (sin payload)
    rpc::Frame resp;
    resp.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
    resp.header.payload_length = 0;
    FileSystem.handleResponse(resp);
}

// --- TEST: PROCESS Y MAILBOX (PROCESS.CPP / MAILBOX.CPP) ---
void test_extreme_process_mailbox() {
    CaptureStream stream;
    setup_test_env(stream);

    // 1. Mailbox: Send con data nula
    Mailbox.send(nullptr, 0);

    // 2. Process: RunAsync con comando nulo o largo
    Process.runAsync(nullptr);
    
    // 3. Process: Poll con PID inválido
    Process.poll(-1);

    // 4. Responses truncadas
    rpc::Frame resp;
    resp.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_RESP);
    resp.header.payload_length = 1; // Debería tener al menos Status + stdout_len + stderr_len
    Process.handleResponse(resp);
}

} // namespace

int main() {
    printf("EXTREME ARDUINO COVERAGE TEST START\n");
    test_extreme_bridge_commands();
    test_extreme_datastore();
    test_extreme_filesystem();
    test_extreme_process_mailbox();
    printf("EXTREME ARDUINO COVERAGE TEST END\n");
    return 0;
}
