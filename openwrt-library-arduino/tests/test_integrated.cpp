#include <stdio.h>
#include <stdint.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "security/security.h"
#include "protocol/rle.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"
#include "test_support.h"
#include "BridgeTestInterface.h"
#include <etl/span.h>

static unsigned long g_test_millis = 0;
unsigned long millis() { 
    return g_test_millis++; 
}

using namespace rpc;
using namespace bridge;

// ... (skip to integrated_test_rle)

void integrated_test_rle() {
    uint8_t in[] = "AAAAABBBCCCC";
    uint8_t enc[32], dec[32];
    size_t el = rle::encode(etl::span<const uint8_t>(in, 12), etl::span<uint8_t>(enc, 32));
    size_t dl = rle::decode(etl::span<const uint8_t>(enc, el), etl::span<uint8_t>(dec, 32));
    TEST_ASSERT(dl == 12 && memcmp(in, dec, 12) == 0);
    
    uint8_t in2[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    el = rle::encode(etl::span<const uint8_t>(in2, 5), etl::span<uint8_t>(enc, 32));
    dl = rle::decode(etl::span<const uint8_t>(enc, el), etl::span<uint8_t>(dec, 32));
    TEST_ASSERT(dl == 5 && memcmp(in2, dec, 5) == 0);
}

void integrated_test_protocol() {
    FrameBuilder b;
    FrameParser p;
    uint8_t raw[128];
    uint8_t pl[] = {0x01, 0x02, 0x03};
    size_t rl = b.build(etl::span<uint8_t>(raw, 128), 0x100, etl::span<const uint8_t>(pl, 3));
    // [SIL-2] etl::expected API
    auto result = p.parse(etl::span<const uint8_t>(raw, rl));
    TEST_ASSERT(result.has_value());
    Frame f = result.value();
    TEST_ASSERT(f.header.command_id == 0x100);
}

void integrated_test_bridge_core() {
    FullMockStream stream;
    BridgeClass localBridge(stream);
    localBridge.begin(115200, "secret");
    auto accessor = bridge::test::TestAccessor::create(localBridge);
    
    rpc::Frame sync;
    sync.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    sync.header.payload_length = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
    etl::fill_n(sync.payload.data(), rpc::RPC_HANDSHAKE_NONCE_LENGTH, uint8_t{0xAA});
    accessor.dispatch(sync);
    TEST_ASSERT(localBridge.isSynchronized());
    
    rpc::Frame gpio;
    gpio.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
    gpio.header.payload_length = 2;
    gpio.payload[0] = 13; gpio.payload[1] = 1;
    accessor.dispatch(gpio);
    
    localBridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, (const uint8_t*)"X", 1);
    accessor.retransmitLastFrame();
}

void integrated_test_components() {
    Console.begin();
    Console.write((uint8_t)'t');
    Console.flush();
    
    #if BRIDGE_ENABLE_DATASTORE
    DataStore.put("k", "v");
    #endif
    #if BRIDGE_ENABLE_MAILBOX
    Mailbox.send("m");
    #endif
    #if BRIDGE_ENABLE_FILESYSTEM
    FileSystem.read("f");
    #endif
    #if BRIDGE_ENABLE_PROCESS
    Process.run("ls");
    #endif
}

void integrated_test_error_branches() {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, "err");
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("flash"));
    Bridge.enterSafeState();
    TEST_ASSERT(rpc::security::run_cryptographic_self_tests());
}

void integrated_test_extreme_coverage() {
    auto accessor = bridge::test::TestAccessor::create(Bridge);

    // 1. Sistema Desconocido
    rpc::Frame f;
    f.header.command_id = 0x4F; 
    f.header.payload_length = 0;
    accessor.dispatch(f);

    // 2. Comandos GPIO payload inválido
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    f.header.payload_length = 0;
    accessor.dispatch(f);

    // 3. GET_CAPABILITIES
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
    f.header.payload_length = 0;
    accessor.dispatch(f);

    // 3b. GET_VERSION
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
    f.header.payload_length = 0;
    accessor.dispatch(f);

    // 3c. GET_FREE_MEMORY
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY);
    f.header.payload_length = 0;
    accessor.dispatch(f);

    // 3d. SET_BAUDRATE
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE);
    f.header.payload_length = 4;
    uint32_t baud = 57600;
    rpc::write_u32_be(f.payload.data(), baud);
    accessor.dispatch(f);

    // 3g. LINK_RESET con config
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET);
    f.header.payload_length = rpc::RPC_HANDSHAKE_CONFIG_SIZE;
    rpc::write_u16_be(f.payload.data(), 500); // ack_timeout
    f.payload[2] = 3; // retry_limit
    rpc::write_u32_be(f.payload.data() + 3, 5000); // resp_timeout
    accessor.dispatch(f);

    // 3e. LINK_SYNC malformado
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
    f.header.payload_length = 1; // Debería ser 16
    accessor.dispatch(f);

    // 3f. Malformed status
    f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED);
    f.header.payload_length = 2;
    rpc::write_u16_be(f.payload.data(), 0x1234);
    accessor.dispatch(f);

    // 4. DataStore Put Nulls
    DataStore.put(nullptr, "v");
    DataStore.put("k", nullptr);

    // 5. FileSystem Put Nulls / Largos
    FileSystem.write(nullptr, (const uint8_t*)"d", 1);
    char long_path[200]; etl::fill_n(long_path, 199, 'a'); long_path[199] = '\0';
    FileSystem.write(long_path, (const uint8_t*)"d", 1);

    // 6. Responses Malformadas
    rpc::Frame resp;
    resp.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_RESP);
    resp.header.payload_length = 1;
    Process.handleResponse(resp);
    
    resp.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
    resp.header.payload_length = 1;
    resp.payload[0] = 50; // Longitud mentirosa
    DataStore.handleResponse(resp);

    // 7. Deduplicación
    rpc::Frame f_dup;
    f_dup.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    f_dup.header.payload_length = 2;
    f_dup.payload[0] = 13; f_dup.payload[1] = 1;
    accessor.dispatch(f_dup);
    accessor.dispatch(f_dup); // Segunda vez (duplicado)

    // 8. Comandos no autorizados (desincronizar primero)
    Bridge.enterSafeState();
    rpc::Frame f_unauth;
    f_unauth.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
    f_unauth.header.payload_length = 2;
    f_unauth.payload[0] = 13; f_unauth.payload[1] = 1;
    accessor.dispatch(f_unauth);
    Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE, (const uint8_t*)"\x0D\x01", 2);
    
    // 9. Compresión (Payload repetitivo)
    accessor.setSynchronized(true);
    uint8_t large_pl[64];
    etl::fill_n(large_pl, 64, uint8_t{'A'});
    Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, large_pl, 64);

    // 10. Más GPIO
    rpc::Frame f_gpio;
    f_gpio.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ);
    f_gpio.header.payload_length = 1;
    f_gpio.payload[0] = 13;
    accessor.dispatch(f_gpio);

    f_gpio.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ);
    f_gpio.header.payload_length = 1;
    f_gpio.payload[0] = 0;
    accessor.dispatch(f_gpio);

    f_gpio.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE);
    f_gpio.header.payload_length = 2;
    f_gpio.payload[0] = 3; f_gpio.payload[1] = 128;
    accessor.dispatch(f_gpio);

    // 11. Console Watermarks y Write Large
    {
        uint8_t large_console_pl[300];
        etl::fill_n(large_console_pl, 300, uint8_t{'C'});
        Console.write(large_console_pl, 300);
    }
    for (int i = 0; i < 40; i++) {
        uint8_t b = (uint8_t)i;
        Console._push(etl::span<const uint8_t>(&b, 1));
    }
    while (Console.available() > 0) {
        Console.read();
    }
    Console.peek(); // Empty peek
    Console.read(); // Empty read

    // 12. FileSystem Casos de Borde
    {
        char huge_path[rpc::RPC_MAX_FILEPATH_LENGTH + 10];
        etl::fill_n(huge_path, sizeof(huge_path), 'P');
        huge_path[sizeof(huge_path)-1] = '\0';
        FileSystem.write(huge_path, (const uint8_t*)"X", 1);
        FileSystem.remove(huge_path);
        FileSystem.read(huge_path);
    }
    
    rpc::Frame f_fs;
    f_fs.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
    f_fs.header.payload_length = 1; // Truncado (necesita 2 + data)
    FileSystem.handleResponse(f_fs);

    f_fs.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE);
    f_fs.header.payload_length = 0; // Malformado
    FileSystem.handleResponse(f_fs);

    // 13. Mailbox Casos de Borde
    {
        char huge_msg[rpc::MAX_PAYLOAD_SIZE + 10];
        etl::fill_n(huge_msg, sizeof(huge_msg), 'M');
        huge_msg[sizeof(huge_msg)-1] = '\0';
        Mailbox.send(huge_msg);
    }
    
    rpc::Frame f_mb;
    f_mb.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
    f_mb.header.payload_length = 1; // Truncado
    Mailbox.handleResponse(f_mb);

    f_mb.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH);
    f_mb.header.payload_length = 1; // Truncado
    Mailbox.handleResponse(f_mb);

    // 14. Process Casos de Borde
    {
        char huge_cmd[rpc::MAX_PAYLOAD_SIZE + 10];
        etl::fill_n(huge_cmd, sizeof(huge_cmd), 'S');
        huge_cmd[sizeof(huge_cmd)-1] = '\0';
        Process.run(huge_cmd);
        Process.runAsync(huge_cmd);
    }
    for (int i = 0; i < 10; i++) Process.poll(i); // Fill queue

    rpc::Frame f_proc;
    f_proc.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_RESP);
    // Success Case Process Run
    // payload: status(1), out_len(2), out..., err_len(2), err...
    uint8_t proc_run_data[] = {0x30, 0, 2, 'O', 'K', 0, 1, 'E'};
    f_proc.header.payload_length = 8;
    memcpy(f_proc.payload.data(), proc_run_data, 8);
    Process.handleResponse(f_proc);

    f_proc.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_RESP);
    f_proc.header.payload_length = 4; // Truncado (necesita status(1) + out_len(2) + out... + err_len(2))
    Process.handleResponse(f_proc);

    f_proc.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
    // Success Case Process Poll
    // payload: status(1), running(1), out_len(2), out..., err_len(2), err...
    uint8_t proc_poll_data[] = {0x30, 0, 0, 1, 'X', 0, 1, 'Y'};
    f_proc.header.payload_length = 8;
    memcpy(f_proc.payload.data(), proc_poll_data, 8);
    Process.handleResponse(f_proc);

    f_proc.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
    f_proc.header.payload_length = 5; // Truncado
    Process.handleResponse(f_proc);

    // 14b. DataStore Casos de Borde
    {
        char huge_key[rpc::RPC_MAX_DATASTORE_KEY_LENGTH + 10];
        etl::fill_n(huge_key, sizeof(huge_key), 'K');
        huge_key[sizeof(huge_key)-1] = '\0';
        DataStore.put(huge_key, "V");
        DataStore.requestGet(huge_key);
    }
    for (int i = 0; i < 10; i++) DataStore.requestGet("key"); // Fill queue
    DataStore.handleResponse(rpc::Frame()); // Unknown command


    // 15. Estatus y Retransmisión
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, "test error");
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("flash error"));
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, (const char*)nullptr);
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, (const __FlashStringHelper*)nullptr);
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, "");

    
    // Simular espera de ACK y retransmisión
    g_test_millis = 1000;
    Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, (const uint8_t*)"R", 1);
    
    // Avanzar tiempo para timeout
    g_test_millis += 500; // default ack timeout is 200ms
    Bridge.process(); // Debería retransmitir
    
    // Avanzar más para superar límite de reintentos
    for (int i = 0; i < 6; i++) {
        g_test_millis += 500;
        Bridge.process();
    }
    // Debería entrar en safe state
    TEST_ASSERT(!Bridge.isSynchronized());
    accessor.setSynchronized(true);

    // 16. Inyectar Basura Serial (Malformed COBS)
    uint8_t garbage[] = {0x01, 0x02, 0x03, 0x00}; // Mal COBS
    g_bridge_stream.inject(garbage, 4); 
    Bridge.process();

    // 17. Frames malformados (CRC mismatch)
    uint8_t bad_crc_frame[] = {0x05, 0x01, 0x00, 0x00, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00}; 
    g_bridge_stream.inject(bad_crc_frame, 10);
    Bridge.process();

    // 17b. Parser Errors Switch
    // Simular error de CRC en parser
    accessor.setSynchronized(true);
    // PacketSerial.update() calls onPacketReceived
    // Let's call onPacketReceived directly with garbage
    uint8_t garbage2[] = {0x01, 0x02};
    Bridge.onPacketReceived(garbage2, 2);
    Bridge.process();


    // 18. GPIO switch cases
    f_gpio.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
    f_gpio.header.payload_length = 2;
    f_gpio.payload[0] = 13; f_gpio.payload[1] = 1;
    accessor.dispatch(f_gpio);

    // 18b. Console Write via Dispatch
    rpc::Frame f_con;
    f_con.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
    f_con.header.payload_length = 1;
    f_con.payload[0] = 'D';
    accessor.dispatch(f_con);

    // 18c. Compressed Frame Malformed
    rpc::Frame f_comp;
    f_comp.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE) | rpc::RPC_CMD_FLAG_COMPRESSED;
    f_comp.header.payload_length = 1;
    f_comp.payload[0] = 0xFF; // Invalid RLE
    accessor.dispatch(f_comp);

    // 18d. Duplicate Console Write
    accessor.dispatch(f_con); // Repetido

    // 18e. Mailbox Push via Dispatch
    rpc::Frame f_mb_p;
    f_mb_p.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH);
    f_mb_p.header.payload_length = 4;
    uint8_t mb_p_data[] = {0, 2, 'O', 'K'};
    memcpy(f_mb_p.payload.data(), mb_p_data, 4);
    accessor.dispatch(f_mb_p);
    accessor.dispatch(f_mb_p); // Duplicado

    // 18f. File Write via Dispatch
    rpc::Frame f_fs_w;
    f_fs_w.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE);
    const char fw_path[] = "test.txt";
    f_fs_w.payload[0] = (uint8_t)strlen(fw_path);
    memcpy(f_fs_w.payload.data() + 1, fw_path, strlen(fw_path));
    f_fs_w.header.payload_length = 1 + (uint16_t)strlen(fw_path) + 1;
    accessor.dispatch(f_fs_w);
    accessor.dispatch(f_fs_w); // Duplicado



    // 19. HardwareSerial Constructor
    {
        BridgeClass bridge_hs(Serial1);
        bridge_hs.begin(115200);
    }

    // 20. DataStore / FileSystem Callbacks
    DataStore.onDataStoreGetResponse([](const char* k, const uint8_t* v, uint16_t l) { (void)k; (void)v; (void)l; });
    FileSystem.onFileSystemReadResponse([](const uint8_t* d, uint16_t l) { (void)d; (void)l; });
    Process.onProcessRunResponse([](rpc::StatusCode s, const uint8_t* out, uint16_t ol, const uint8_t* err, uint16_t el) { (void)s; (void)out; (void)ol; (void)err; (void)el; });
    Process.onProcessPollResponse([](rpc::StatusCode s, uint8_t ec, const uint8_t* out, uint16_t ol, const uint8_t* err, uint16_t el) { (void)s; (void)ec; (void)out; (void)ol; (void)err; (void)el; });
    Process.onProcessRunAsyncResponse([](int16_t p) { (void)p; });
    Mailbox.onMailboxMessage([](const uint8_t* m, uint16_t l) { (void)m; (void)l; });
    Mailbox.onMailboxAvailableResponse([](uint16_t c) { (void)c; });
}

int main() {
    printf("INTEGRATED ARDUINO TEST START\n");
    fflush(stdout);
    
    Bridge.begin(115200);
    // Synchronize global bridge for component tests
    bridge::test::TestAccessor::create(Bridge).setIdle();

    printf("Running: integrated_test_rle\n"); fflush(stdout);
    integrated_test_rle();
    
    printf("Running: integrated_test_protocol\n"); fflush(stdout);
    integrated_test_protocol();
    
    printf("Running: integrated_test_bridge_core\n"); fflush(stdout);
    integrated_test_bridge_core();
    
    printf("Running: integrated_test_components\n"); fflush(stdout);
    integrated_test_components();
    
    printf("Running: integrated_test_error_branches\n"); fflush(stdout);
    integrated_test_error_branches();
    
    printf("Running: integrated_test_extreme_coverage\n"); fflush(stdout);
    integrated_test_extreme_coverage();
    
    printf("INTEGRATED ARDUINO TEST END\n");
    fflush(stdout);
    return 0;
}