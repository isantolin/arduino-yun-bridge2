#include <Arduino.h>
#include <unity.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "test_support.h"
#include "protocol/rpc_structs.h"
#include "util/pb_copy.h"

using namespace bridge::test;

// Global Mocks and Objects
HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
ConsoleClass Console;
#if BRIDGE_ENABLE_DATASTORE
DataStoreClass DataStore;
#endif
#if BRIDGE_ENABLE_MAILBOX
MailboxClass Mailbox;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
FileSystemClass FileSystem;
#endif
#if BRIDGE_ENABLE_PROCESS
ProcessClass Process;
#endif

// Globals for tests
static BiStream g_stream;
static TestAccessor ba = TestAccessor::create(Bridge);

void setUp(void) {
    reset_bridge_core(Bridge, g_stream);
    g_stream.clear();
}

void tearDown(void) {}

// --- 1. Forzar fallos en Despachadores O(1) ---
void test_dispatch_out_of_bounds() {
    rpc::Frame f = {};
    
    // System command justo fuera de rango (STRIDE=2)
    f.header.command_id = 76; // RPC_SYSTEM_COMMAND_MAX + 1
    ba.dispatch(f); // Debe caer en onUnknownCommand
    
    // Status command justo fuera de rango (Count=9)
    f.header.command_id = 57; // RPC_STATUS_CODE_MIN + 9
    ba.dispatch(f);
    
    // GPIO command justo fuera de rango
    f.header.command_id = 96; // RPC_GPIO_COMMAND_MAX + 1
    ba.dispatch(f);
}

// --- 2. Probar pb_copy_join exhaustivamente ---
void test_pb_copy_join_edge_cases() {
    char dst[16];
    
    // Caso 1: Solo base
    rpc::util::pb_copy_join("base", etl::span<const etl::string_view>(), dst, sizeof(dst));
    TEST_ASSERT_EQUAL_STRING("base", dst);
    
    // Caso 2: Base + 1 parte
    const etl::string_view parts1[] = {"p1"};
    rpc::util::pb_copy_join("b", etl::span<const etl::string_view>(parts1, 1), dst, sizeof(dst));
    TEST_ASSERT_EQUAL_STRING("b p1", dst);
    
    // Caso 3: Desbordamiento total
    rpc::util::pb_copy_join("base_very_long_string", etl::span<const etl::string_view>(), dst, sizeof(dst));
    TEST_ASSERT_EQUAL_INT(15, strlen(dst));
    
    // Caso 4: Desbordamiento en la unión
    const etl::string_view parts2[] = {"long_part_that_overflows"};
    rpc::util::pb_copy_join("b", etl::span<const etl::string_view>(parts2, 1), dst, sizeof(dst));
    TEST_ASSERT_EQUAL_INT(15, strlen(dst));
}

// --- 3. Forzar errores en FileSystem (HAL False) ---
// Nota: Requiere que hal.cpp devuelva false. En host tests, hal::hasSD() es true por defecto.
// Pero podemos probar el envío cuando falla sendPbCommand.
void test_filesystem_hal_error_paths() {
#if BRIDGE_ENABLE_FILESYSTEM
    // Forzar desbordamiento de ruta para cubrir rpc::util::pb_copy_string fallando internamente 
    // (aunque es difícil si no exponemos el error, cubriremos la llamada).
    FileSystem.write("path", etl::span<const uint8_t>());
    FileSystem.read("path", FileSystemClass::FileSystemReadHandler());
    FileSystem.remove("path");
#endif
}

// --- 4. Forzar ramas de error en el Bridge (Critical Paths) ---
void test_bridge_critical_error_branches() {
    // Retransmitir cuando no hay nada en la cola
    ba.handleAck(rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION)); // trigger _finalize
    
    // Enviar status malformado con ID que no coincide con el último
    ba.setIdle();
    ba.handleMalformed(999); 

    // Forzar retransmisión real
    rpc::payload::SetBaudratePacket msg = {115200};
    Bridge.sendPbCommand(rpc::CommandId::CMD_SET_BAUDRATE, msg);
    ba.onAckTimeout(); // Debe retransmitir
}

// --- 5. Probar rpc::Payload::parse fallando ---
void test_payload_parse_malformed() {
    rpc::Frame f = {};
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    f.header.payload_length = 1; // Demasiado corto para LinkSync
    f.payload[0] = 0xFF;
    
    auto res = rpc::Payload::parse<rpc::payload::LinkSync>(f);
    TEST_ASSERT_FALSE(res.has_value());
}

// --- 6. Probar todos los helpers de Bridge.h ---
void test_bridge_helpers_coverage() {
    ba.setIdle();
    
    rpc::payload::DatastoreGet datastore_get = {};
    rpc::util::pb_copy_string("testkey", datastore_get.key, sizeof(datastore_get.key));
    Bridge.sendPbCommand(rpc::CommandId::CMD_DATASTORE_GET, datastore_get);

    rpc::payload::FileRead file_read = {};
    rpc::util::pb_copy_string("testfile", file_read.path, sizeof(file_read.path));
    Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_READ, file_read);

    rpc::payload::FileRemove file_remove = {};
    rpc::util::pb_copy_string("testfile", file_remove.path, sizeof(file_remove.path));
    Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_REMOVE, file_remove);

    uint8_t data[] = {0xDE, 0xAD};
    rpc::payload::DatastorePut datastore_put = {};
    rpc::util::pb_copy_string("testkey", datastore_put.key, sizeof(datastore_put.key));
    rpc::util::pb_setup_encode_span(datastore_put.value, etl::span<const uint8_t>(data, 2));
    Bridge.sendPbCommand(rpc::CommandId::CMD_DATASTORE_PUT, datastore_put);

    rpc::payload::FileWrite file_write = {};
    rpc::util::pb_copy_string("testfile", file_write.path, sizeof(file_write.path));
    rpc::util::pb_setup_encode_span(file_write.data, etl::span<const uint8_t>(data, 2));
    Bridge.sendPbCommand(rpc::CommandId::CMD_FILE_WRITE, file_write);

    rpc::payload::MailboxPush mailbox_push = {};
    rpc::util::pb_setup_encode_span(mailbox_push.data, etl::span<const uint8_t>(data, 2));
    Bridge.sendPbCommand(rpc::CommandId::CMD_MAILBOX_PUSH, mailbox_push);

    // 4. sendPbFrame (Status with payload)
    rpc::payload::AckPacket ack = {rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE)};
    Bridge.sendPbFrame(rpc::StatusCode::STATUS_ACK, ack);
}

// --- 7. Lógica de Capabilities y HAL ---
void test_capabilities_dispatch_coverage() {
    rpc::Frame f = {};
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
    f.header.payload_length = 0;
    ba.dispatch(f);
}

// --- 8. rpc::Payload::parse Exhaustivo ---
void test_payload_parse_all_descriptors() {
    rpc::Frame f = {};
    
    // Probar parseo de diversos tipos para cubrir rpc_structs.h REGISTER_DESCRIPTOR
    rpc::payload::Capabilities caps = {};
    rpc::Payload::parse<rpc::payload::Capabilities>(f, caps);
    
    rpc::payload::VersionResponse ver = {};
    rpc::Payload::parse<rpc::payload::VersionResponse>(f, ver);
    
    rpc::payload::MailboxAvailableResponse mb_avail = {};
    rpc::Payload::parse<rpc::payload::MailboxAvailableResponse>(f, mb_avail);
    
    rpc::payload::ProcessPollResponse proc_poll = {};
    rpc::Payload::parse<rpc::payload::ProcessPollResponse>(f, proc_poll);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_dispatch_out_of_bounds);
    RUN_TEST(test_pb_copy_join_edge_cases);
    RUN_TEST(test_filesystem_hal_error_paths);
    RUN_TEST(test_bridge_critical_error_branches);
    RUN_TEST(test_payload_parse_malformed);
    RUN_TEST(test_bridge_helpers_coverage);
    RUN_TEST(test_capabilities_dispatch_coverage);
    RUN_TEST(test_payload_parse_all_descriptors);
    return UNITY_END();
}

Stream* g_arduino_stream_delegate = nullptr;
