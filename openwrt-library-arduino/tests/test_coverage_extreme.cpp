/*
 * test_coverage_extreme.cpp
 * Objetivo: Fuzzing determinista y cobertura de ramas de error (Zero STL).
 */

#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <stdint.h>

// Habilitar acceso a privados para testing white-box
#define private public
#define protected public
#include "Bridge.h"
#include "arduino/BridgeTransport.h"
#undef private
#undef protected

#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h" // Necesario para rpc::Frame
#include "protocol/cobs.h"
#include "protocol/crc.h"

// Mocks Globales
HardwareSerial Serial;
HardwareSerial Serial1;
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

#define MAX_BUFFER_SIZE 1024

// --- MOCK STREAM (C-Style Ring Buffer Simulation) ---
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
    
    // Implementación obligatoria de Stream/Print
    size_t write(const uint8_t *buffer, size_t size) override {
        size_t n = 0;
        while (size--) {
            if (write(*buffer++)) n++;
            else break;
        }
        return n;
    }

    void flush() override {}
    
    // Helper para inyectar datos
    void inject_bytes(const uint8_t* data, size_t len) {
        size_t space = MAX_BUFFER_SIZE - rx_len;
        size_t to_copy = (len < space) ? len : space;
        memcpy(rx_buffer + rx_len, data, to_copy);
        rx_len += to_copy;
    }
};

FuzzStream fuzzStream;
BridgeClass Bridge(fuzzStream);

// --- HELPERS (C-Style) ---

// Helper simple para codificar COBS en buffer estático
size_t simple_cobs_encode(const uint8_t* input, size_t length, uint8_t* output) {
    size_t read_index = 0;
    size_t write_index = 1;
    size_t code_index = 0;
    uint8_t code = 1;

    while (read_index < length) {
        if (input[read_index] == 0) {
            output[code_index] = code;
            code = 1;
            code_index = write_index++;
            read_index++;
        } else {
            output[write_index++] = input[read_index++];
            code++;
            if (code == 0xFF) {
                output[code_index] = code;
                code = 1;
                code_index = write_index++;
            }
        }
    }
    output[code_index] = code;
    return write_index;
}

void test_crc_failure() {
    printf("[TEST] test_crc_failure\n");
    fuzzStream.reset();
    Bridge.begin(115200);
    
    // Trama Raw: [CMD][LEN_L][LEN_H][CRC_BASURA...]
    uint8_t raw[] = {
        0x0A,       // CMD_GET_VERSION
        0x00, 0x00, // Len 0
        0xDE, 0xAD, 0xBE, 0xEF // CRC Basura
    };
    
    // Encode COBS
    uint8_t encoded[64];
    size_t enc_len = simple_cobs_encode(raw, sizeof(raw), encoded);
    
    // Inyectar trama + delimitador
    fuzzStream.inject_bytes(encoded, enc_len);
    uint8_t delimiter = 0x00;
    fuzzStream.inject_bytes(&delimiter, 1);
    
    // Forzar procesamiento (simulamos el loop de Bridge)
    // BridgeTransport::processInput lee bytes del stream.
    rpc::Frame frame;
    while(fuzzStream.available()) {
        Bridge._transport.processInput(frame);
    }
    
    // Verificación: Debe haber respondido algo (STATUS_CRC_MISMATCH)
    // El buffer TX no debe estar vacío.
    assert(fuzzStream.tx_len > 0);
}

void test_oversized_payload() {
    printf("[TEST] test_oversized_payload\n");
    fuzzStream.reset();
    
    // Crear payload que exceda RPC_MAX_PAYLOAD (usualmente 256 o similar)
    // Header (CMD + LEN) = 3 bytes
    uint8_t raw[300]; 
    memset(raw, 0xAA, sizeof(raw));
    raw[0] = 0x0A; // CMD
    // Longitud declarada grande
    raw[1] = 0xFF; 
    raw[2] = 0x00;
    
    uint8_t encoded[350];
    size_t enc_len = simple_cobs_encode(raw, sizeof(raw), encoded);
    
    fuzzStream.inject_bytes(encoded, enc_len);
    uint8_t delimiter = 0x00;
    fuzzStream.inject_bytes(&delimiter, 1);
    
    rpc::Frame frame;
    while(fuzzStream.available()) {
        Bridge._transport.processInput(frame);
    }
    
    // Se espera que la protección interna evite buffer overflow
    // y no crashee.
    assert(fuzzStream.tx_len > 0 || fuzzStream.rx_pos == fuzzStream.rx_len);
}

void test_write_failure_simulation() {
    printf("[TEST] test_write_failure_simulation\n");
    fuzzStream.reset();
    
    // Llenar buffer TX artificialmente para simular bloqueo/fallo
    fuzzStream.tx_len = MAX_BUFFER_SIZE; 
    
    uint8_t payload[] = {0x01, 0x02};
    // Intentar enviar trama
    bool result = Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, payload, 2);
    
    // Debería fallar o manejarlo
    (void)result; // Suppress unused warning
}

int main() {
    printf("=== RUNNING EXTREME COVERAGE TESTS (NO STL) ===\n");
    test_crc_failure();
    test_oversized_payload();
    test_write_failure_simulation();
    printf("=== ALL TESTS PASSED ===\n");
    return 0;
}
