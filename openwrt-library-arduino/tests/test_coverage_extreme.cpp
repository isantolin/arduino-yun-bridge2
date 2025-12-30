/*
 * test_coverage_extreme.cpp
 * Objetivo: Fuzzing y Simulación de Tiempo (Retransmisiones)
 */

#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <stdint.h>

// --- TIME SIMULATION HOOKS ---
static unsigned long _mock_millis = 0;
// Sobrescribimos millis para los tests
unsigned long millis() {
    return _mock_millis;
}
void advance_millis(unsigned long ms) {
    _mock_millis += ms;
}

// Habilitar acceso a privados
#define private public
#define protected public
#include "Bridge.h"
#include "arduino/BridgeTransport.h"
#undef private
#undef protected

#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"
#include "protocol/cobs.h"

// Mocks Globales
HardwareSerial Serial;
HardwareSerial Serial1;
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

#define MAX_BUFFER_SIZE 1024

// --- MOCK STREAM ---
class FuzzStream : public Stream {
public:
    uint8_t rx_buffer[MAX_BUFFER_SIZE];
    size_t rx_len;
    size_t rx_pos;
    uint8_t tx_buffer[MAX_BUFFER_SIZE];
    size_t tx_len;

    FuzzStream() : rx_len(0), rx_pos(0), tx_len(0) {}

    void reset() {
        rx_len = 0;
        rx_pos = 0;
        tx_len = 0;
        memset(rx_buffer, 0, MAX_BUFFER_SIZE);
        memset(tx_buffer, 0, MAX_BUFFER_SIZE);
    }

    int available() override {
        return (int)(rx_len - rx_pos);
    }

    int read() override {
        if (rx_pos < rx_len) {
            return rx_buffer[rx_pos++];
        }
        return -1;
    }

    int peek() override {
        if (rx_pos < rx_len) {
            return rx_buffer[rx_pos];
        }
        return -1;
    }

    size_t write(uint8_t c) override {
        if (tx_len < MAX_BUFFER_SIZE) {
            tx_buffer[tx_len++] = c;
            return 1;
        }
        return 0;
    }

    size_t write(const uint8_t *buffer, size_t size) override {
        size_t n = 0;
        while (size--) {
            if (write(*buffer++)) n++;
            else break;
        }
        return n;
    }

    void flush() override {}

    void inject_bytes(const uint8_t* data, size_t len) {
        size_t space = MAX_BUFFER_SIZE - rx_len;
        size_t to_copy = (len < space) ? len : space;
        memcpy(rx_buffer + rx_len, data, to_copy);
        rx_len += to_copy;
    }
};

FuzzStream fuzzStream;
BridgeClass Bridge(fuzzStream);

void test_retransmission_logic() {
    printf("[TEST] test_retransmission_logic\n");
    fuzzStream.reset();
    _mock_millis = 1000;

    // 1. Enviar trama que requiere ACK (ej: Console Write)
    uint8_t payload[] = { 'H', 'i' };
    Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, payload, 2);

    assert(Bridge._awaiting_ack == true);
    assert(Bridge._retry_count == 0);
    assert(fuzzStream.tx_len > 0);

    size_t initial_tx_len = fuzzStream.tx_len;

    // 2. Avanzar tiempo más allá del timeout
    advance_millis(Bridge._ack_timeout_ms + 10);

    // Ejecutar process para disparar el check de timeout
    rpc::Frame dummy;
    (void)dummy; // Suppress unused
    Bridge._processAckTimeout();

    // Verificar que retransmitió
    assert(Bridge._retry_count == 1);
    assert(fuzzStream.tx_len > initial_tx_len);

    // 3. Agotar reintentos
    Bridge._retry_count = Bridge._ack_retry_limit;
    advance_millis(Bridge._ack_timeout_ms + 10);
    Bridge._processAckTimeout();

    // Debe haberse rendido
    assert(Bridge._awaiting_ack == false);
}

void test_malformed_response_triggers_retransmit() {
    printf("[TEST] test_malformed_response_triggers_retransmit\n");
    fuzzStream.reset();
    _mock_millis = 2000;

    // Enviar comando
    uint8_t payload[] = { 0x01 };
    Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, payload, 1);
    Bridge._retry_count = 0;
    size_t tx_mark = fuzzStream.tx_len;

    // Simular recepción de STATUS_MALFORMED (0x02)
    Bridge._handleMalformed(Bridge._last_command_id);

    assert(Bridge._retry_count == 1);
    assert(fuzzStream.tx_len > tx_mark);
}

void test_system_command_boundary() {
    printf("[TEST] test_system_command_boundary\n");

    rpc::Frame frame;
    frame.header.payload_length = 0;
    frame.payload = NULL; // Use NULL standard macro

    // 0x0A = GET_VERSION (System)
    frame.header.command_id = 0x0A;
    fuzzStream.reset();
    Bridge.dispatch(frame);
    assert(fuzzStream.tx_len > 0);

    // 0x10 = Unknown -> Should emit UNKNOWN
    fuzzStream.reset();
    frame.header.command_id = 0x10;
    Bridge.dispatch(frame);
    assert(fuzzStream.tx_len > 0);
}

int main() {
    printf("=== RUNNING EXTREME COVERAGE TESTS (V3 Fixed) ===\n");
    test_retransmission_logic();
    test_malformed_response_triggers_retransmit();
    test_system_command_boundary();
    printf("=== ALL TESTS PASSED ===\n");
    return 0;
}
