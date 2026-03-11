#include <stdint.h>
#include <stddef.h>

// Simulated Arduino/Host testing environment
#include <iostream>
#include <chrono>

// SIL-2: No STL dynamic allocation, using ETL exclusively
#include <etl/vector.h>
#include <etl/array.h>
#include <etl/crc32.h>
#include <etl/span.h>

// --- V3 PoC: In-Place COBS Decoding ---
// Decodes a COBS framed buffer directly into the same memory, saving 50% RAM.
size_t cobs_decode_in_place(etl::span<uint8_t> buffer) {
    if (buffer.empty()) return 0;
    
    size_t read_index = 0;
    size_t write_index = 0;
    uint8_t code;
    uint8_t i;
    const size_t length = buffer.size();

    while (read_index < length) {
        code = buffer[read_index];
        
        if (read_index + code > length && code != 1) {
            return 0; // Malformed
        }
        
        read_index++;
        
        for (i = 1; i < code; i++) {
            buffer[write_index++] = buffer[read_index++];
        }
        
        if (code != 0xFF && read_index != length) {
            buffer[write_index++] = '\0';
        }
    }
    
    return write_index;
}

// Standard COBS decode (V2 style) for comparison
size_t cobs_decode_v2(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
    size_t read_index = 0;
    size_t write_index = 0;
    uint8_t code;
    uint8_t i;
    const size_t length = src.size();

    while (read_index < length) {
        code = src[read_index];
        if (read_index + code > length && code != 1) return 0;
        read_index++;
        for (i = 1; i < code; i++) {
            dst[write_index++] = src[read_index++];
        }
        if (code != 0xFF && read_index != length) {
            dst[write_index++] = '\0';
        }
    }
    return write_index;
}

// --- V3 PoC: Fletcher-16 (Lightweight Checksum) ---
uint16_t fletcher16(etl::span<const uint8_t> data) {
    uint16_t sum1 = 0;
    uint16_t sum2 = 0;
    for (size_t i = 0; i < data.size(); ++i) {
        sum1 = (sum1 + data[i]) % 255;
        sum2 = (sum2 + sum1) % 255;
    }
    return (sum2 << 8) | sum1;
}

// --- V3 PoC: Zero-Copy Struct Cast ---
// Ensuring 4-byte alignment
#pragma pack(push, 1)
struct V3TelemetryPayload {
    uint8_t command_id; // 1 byte
    uint8_t padding[3]; // 3 bytes padding to align next 32-bit float
    float temperature;  // 4 bytes
    uint32_t timestamp; // 4 bytes
};
#pragma pack(pop)

int main() {
    std::cout << "--- Arduino MCU Bridge V3 Proof of Concept (ETL-SIL2) ---" << std::endl;

    // 1. Benchmark Checksums
    const size_t bench_len = 64; // typical payload
    etl::vector<uint8_t, 256> dummy_data(bench_len, 0xAB);
    const int iter = 1000000;

    auto t1 = std::chrono::high_resolution_clock::now();
    uint32_t dummy32 = 0;
    for (int i=0; i<iter; i++) {
        etl::crc32 crc_calc;
        crc_calc.add(dummy_data.begin(), dummy_data.end());
        dummy32 ^= crc_calc.value();
    }
    auto t2 = std::chrono::high_resolution_clock::now();
    
    auto t3 = std::chrono::high_resolution_clock::now();
    uint16_t dummy16 = 0;
    for (int i=0; i<iter; i++) dummy16 ^= fletcher16(etl::span<const uint8_t>(dummy_data.data(), bench_len));
    auto t4 = std::chrono::high_resolution_clock::now();

    std::cout << "\n[1] CPU Cycles Checksum (1M iterations over 64 bytes):" << std::endl;
    std::cout << "V2 (ETL CRC-32): " << std::chrono::duration_cast<std::chrono::milliseconds>(t2 - t1).count() << " ms" << std::endl;
    std::cout << "V3 (Fletcher-16): " << std::chrono::duration_cast<std::chrono::milliseconds>(t4 - t3).count() << " ms" << std::endl;

    // 2. In-place COBS
    const uint8_t encoded_raw[] = {0x04, 0x11, 0x22, 0x33, 0x02, 0x44, 0x05, 0x55, 0x66, 0x77, 0x88};
    
    etl::vector<uint8_t, 32> encoded_cobs(encoded_raw, encoded_raw + sizeof(encoded_raw));
    etl::vector<uint8_t, 32> inplace_buffer = encoded_cobs;
    etl::vector<uint8_t, 32> out_buffer;
    out_buffer.resize(encoded_cobs.size());

    size_t len_v2 = cobs_decode_v2(
        etl::span<const uint8_t>(encoded_cobs.data(), encoded_cobs.size()), 
        etl::span<uint8_t>(out_buffer.data(), out_buffer.size())
    );
    
    size_t len_v3 = cobs_decode_in_place(
        etl::span<uint8_t>(inplace_buffer.data(), inplace_buffer.size())
    );

    std::cout << "\n[2] COBS Decoding RAM:" << std::endl;
    std::cout << "V2 used " << (encoded_cobs.capacity() + out_buffer.capacity()) << " bytes." << std::endl;
    std::cout << "V3 used " << (inplace_buffer.capacity()) << " bytes (In-place, 50% savings)." << std::endl;
    std::cout << "Decoded length match: " << (len_v2 == len_v3 ? "YES" : "NO") << std::endl;

    // 3. Zero-Copy Struct
    const uint8_t aligned_raw[] = {0x54, 0x00, 0x00, 0x00, /* temp: 25.5 */ 0x00, 0x00, 0xCC, 0x41, /* ts: 1000 */ 0xE8, 0x03, 0x00, 0x00};
    etl::vector<uint8_t, 32> aligned_buffer(aligned_raw, aligned_raw + sizeof(aligned_raw));
    
    // Cast O(1)
    const V3TelemetryPayload* payload = reinterpret_cast<const V3TelemetryPayload*>(aligned_buffer.data());
    
    std::cout << "\n[3] Zero-Copy Presentation:" << std::endl;
    std::cout << "Command ID: 0x" << std::hex << (int)payload->command_id << std::dec << std::endl;
    std::cout << "Temperature: " << payload->temperature << std::endl;
    std::cout << "Timestamp: " << payload->timestamp << std::endl;
    std::cout << "Parsing Cost: 0 bytes copied, O(1) CPU time." << std::endl;

    return 0;
}
