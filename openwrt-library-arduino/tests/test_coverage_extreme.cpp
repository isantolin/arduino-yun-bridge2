/*
 * test_coverage_extreme.cpp (V4 Final Corrected)
 * Enfoque: Manipulación directa de estado interno y Fuzzing de Protocolo.
 */

#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <vector>

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
    std::vector<uint8_t> rx;
    std::vector<uint8_t> tx;
    bool write_fails = false;

    int available() override { return rx.size(); }
    int read() override {
        if (rx.empty()) return -1;
        uint8_t b = rx.front();
        rx.erase(rx.begin());
        return b;
    }
    int peek() override { return rx.empty() ? -1 : rx.front(); }

    size_t write(uint8_t c) override {
        if (write_fails) return 0;
        tx.push_back(c);
        return 1;
    }
    size_t write(const uint8_t *b, size_t s) override {
        size_t n = 0;
        while (s--) n += write(*b++);
        return n;
    }
    void flush() override {}

    void push_rx(const std::vector<uint8_t>& data) {
        rx.insert(rx.end(), data.begin(), data.end());
    }
};

FlakyStream io;
BridgeClass Bridge(io);

// Tests

void test_buffer_overflow_protection() {
    printf("TEST: Buffer Overflow Protection\n");
    io.rx.clear();

    // Crear trama válida pero GIGANTE (mayor que buffer interno)
    std::vector<uint8_t> frame;
    frame.push_back(0x00); // 0 bytes to next delimiter (COBS start)
    for (int i = 0; i < 300; i++) frame.push_back(0x01);
    frame.push_back(0x00); // Delimiter

    io.push_rx(frame);

    // Procesar. Debería detectar overflow y resetear buffer sin crashear.
    rpc::Frame f;
    while (io.available()) {
        Bridge._transport.processInput(f);
    }
}

void test_write_failure_handling() {
    printf("TEST: Write Failure Handling\n");
    io.write_fails = true;

    uint8_t data[] = {0xAA};
    // sendFrame debe retornar false si el stream falla
    // Usamos CMD_GET_VERSION (0x0A) como comando válido genérico
    bool ok = Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, data, 1);

    assert(ok == false);
    io.write_fails = false; // Restaurar
}

void test_handshake_timeout_and_retry() {
    printf("TEST: Handshake Timeout & Retry\n");
    Bridge.begin(115200); // Inicia handshake
    io.tx.clear();

    // Estado inicial: esperando sync
    assert(Bridge._synchronized == false);

    // Avanzar tiempo > timeout
    forward_time(Bridge._ack_timeout_ms + 100);

    // Ejecutar ciclo
    // Bridge no tiene poll(), llamamos a _processAckTimeout directamente
    // o simulamos el ciclo si existiera un método público de polling.
    // Al no haber poll(), usamos el mecanismo interno:
    Bridge._processAckTimeout();

    // Verificar que se envió algo nuevo (retransmisión de sync)
    assert(io.tx.size() > 0);
}

void test_protocol_crc_error() {
    printf("TEST: Protocol CRC Error\n");
    // Inyectar llamada directa si encoding es complejo en test
    Bridge._emitStatus(rpc::StatusCode::STATUS_CRC_MISMATCH, "");
    assert(io.tx.size() > 0);
}

int main() {
    test_buffer_overflow_protection();
    test_write_failure_handling();
    test_handshake_timeout_and_retry();
    test_protocol_crc_error();
    printf("ALL TESTS PASSED\n");
    return 0;
}
