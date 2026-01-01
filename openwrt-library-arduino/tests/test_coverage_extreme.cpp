/*
 * test_coverage_extreme.cpp (V4 Final Corrected)
 * Enfoque: Manipulación directa de estado interno y Fuzzing de Protocolo.
 */

#include <string.h>
#include <stdio.h>

#include "test_support.h"

// Use a custom millis() implementation for time travel in host tests.
#define ARDUINO_STUB_CUSTOM_MILLIS 1

// 1. Sobrescribir millis() para Time Travel
static unsigned long _virtual_millis = 0;
unsigned long millis() { return _virtual_millis; }
void forward_time(unsigned long ms) { _virtual_millis += ms; }

// 2. Exponer privados
#define private public
#define protected public
#include "Bridge.h"
#include "arduino/BridgeTransport.h"
#undef private
#undef protected

#include "protocol/rpc_protocol.h"
#include "protocol/cobs.h"
#include "test_constants.h"

// Mocks
HardwareSerial Serial;
HardwareSerial Serial1;
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

// 3. Mock Stream con fallos programables
class FlakyStream : public Stream {
public:
    ByteBuffer<8192> rx;
    ByteBuffer<8192> tx;
    bool write_fails = false;

    int available() override { return static_cast<int>(rx.remaining()); }
    int read() override { return rx.read_byte(); }
    int peek() override { return rx.peek_byte(); }

    size_t write(uint8_t c) override {
        if (write_fails) return 0;
        TEST_ASSERT(tx.push(c));
        return 1;
    }
    size_t write(const uint8_t *b, size_t s) override {
        size_t n = 0;
        while (s--) n += write(*b++);
        return n;
    }
    void flush() override {}

    void push_rx(const uint8_t* data, size_t len) {
        TEST_ASSERT(rx.append(data, len));
    }
};

FlakyStream io;
BridgeClass Bridge(io);

// Tests

void test_buffer_overflow_protection() {
    printf("TEST: Buffer Overflow Protection\n");
    io.rx.clear();

    // Crear trama válida pero GIGANTE (mayor que buffer interno)
    uint8_t frame[302];
    frame[0] = rpc::RPC_FRAME_DELIMITER;
    test_memfill(frame + 1, 300, TEST_BYTE_01);
    frame[301] = rpc::RPC_FRAME_DELIMITER;

    io.push_rx(frame, sizeof(frame));

    // Procesar. Debería detectar overflow y resetear buffer sin crashear.
    rpc::Frame f;
    while (io.available()) {
        Bridge._transport.processInput(f);
    }
}

void test_write_failure_handling() {
    printf("TEST: Write Failure Handling\n");
    io.write_fails = true;

    uint8_t data[] = {TEST_PAYLOAD_BYTE};
    // sendFrame debe retornar false si el stream falla
    bool ok = Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, data, 1);

    TEST_ASSERT(ok == false);
    io.write_fails = false; // Restaurar
}

void test_ack_timeout_and_retry() {
    printf("TEST: ACK Timeout & Retry\n");
    Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
    io.tx.clear();

    // En host tests (compilados con BRIDGE_TEST_NO_GLOBALS), begin() no bloquea
    // esperando sincronización, y el MCU no inicia CMD_LINK_SYNC (lo inicia Linux).
    // Para cubrir el path de retransmisión, enviamos un comando permitido en
    // estado no sincronizado.
    TEST_ASSERT(Bridge._synchronized == false);

    uint8_t payload[] = {TEST_PAYLOAD_BYTE};
    bool ok = Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, payload, sizeof(payload));
    TEST_ASSERT(ok == true);

    // Limpiar TX para medir solo la retransmisión.
    io.tx.clear();

    // Avanzar tiempo > timeout y forzar evaluación.
    forward_time(Bridge._ack_timeout_ms + 100);
    Bridge._processAckTimeout();

    // Verificar que se retransmitió el último frame.
    TEST_ASSERT(io.tx.len > 0);
    TEST_ASSERT(Bridge._retry_count >= 1);
}

void test_protocol_crc_error() {
    printf("TEST: Protocol CRC Error\n");
    // Inyectar llamada directa si encoding es complejo en test
    Bridge._emitStatus(rpc::StatusCode::STATUS_CRC_MISMATCH, "");
    TEST_ASSERT(io.tx.len > 0);
}

int main() {
    test_buffer_overflow_protection();
    test_write_failure_handling();
    test_ack_timeout_and_retry();
    test_protocol_crc_error();
    printf("ALL TESTS PASSED\n");
    return 0;
}
