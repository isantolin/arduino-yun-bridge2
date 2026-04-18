#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "protocol/rpc_structs.h"
#include "test_support.h"

// Global stubs
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

template <typename T>
void test_roundtrip(const T& p) {
    uint8_t buffer[rpc::MAX_PAYLOAD_SIZE];
    msgpack::Encoder enc(buffer, rpc::MAX_PAYLOAD_SIZE);
    TEST_ASSERT(p.encode(enc));
    
    T p2 = {};
    msgpack::Decoder dec(buffer, enc.size());
    TEST_ASSERT(p2.decode(dec));
}

void test_all_structs_roundtrip() {
    test_roundtrip(rpc::payload::VersionResponse{1, 2, 3});
    test_roundtrip(rpc::payload::FreeMemoryResponse{1024});
    test_roundtrip(rpc::payload::Capabilities{1, 2, 20, 6, 0xFF});
    test_roundtrip(rpc::payload::PinMode{13, 1});
    test_roundtrip(rpc::payload::DigitalWrite{2, 1});
    test_roundtrip(rpc::payload::AnalogWrite{5, 128});
    test_roundtrip(rpc::payload::PinRead{7});
    test_roundtrip(rpc::payload::DigitalReadResponse{1});
    test_roundtrip(rpc::payload::AnalogReadResponse{512});
    
    const char* str = "test";
    rpc::payload::ConsoleWrite cw;
    cw.data = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(cw);
    
    rpc::payload::DatastorePut dp;
    dp.key = etl::span<const char>(str, 4);
    dp.value = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(dp);
    
    rpc::payload::DatastoreGet dg;
    dg.key = etl::span<const char>(str, 4);
    test_roundtrip(dg);
    
    rpc::payload::DatastoreGetResponse dgr;
    dgr.value = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(dgr);
    
    rpc::payload::MailboxPush mbp;
    mbp.data = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(mbp);
    
    test_roundtrip(rpc::payload::MailboxProcessed{1});
    test_roundtrip(rpc::payload::MailboxAvailableResponse{5});
    
    rpc::payload::MailboxReadResponse mbr;
    mbr.content = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(mbr);
    
    rpc::payload::FileWrite fw;
    fw.path = etl::span<const char>(str, 4);
    fw.data = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(fw);
    
    rpc::payload::FileRead fr;
    fr.path = etl::span<const char>(str, 4);
    test_roundtrip(fr);
    
    rpc::payload::FileRemove frm;
    frm.path = etl::span<const char>(str, 4);
    test_roundtrip(frm);
    
    rpc::payload::FileReadResponse frr;
    frr.content = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(frr);
    
    rpc::payload::ProcessRunAsync pra;
    pra.command = etl::span<const char>(str, 4);
    test_roundtrip(pra);
    
    test_roundtrip(rpc::payload::ProcessRunAsyncResponse{123});
    test_roundtrip(rpc::payload::ProcessPoll{123});
    
    rpc::payload::ProcessPollResponse ppr;
    ppr.stdout_data = etl::span<const uint8_t>((const uint8_t*)str, 4);
    ppr.stderr_data = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(ppr);
    
    test_roundtrip(rpc::payload::ProcessKill{123});
    test_roundtrip(rpc::payload::AckPacket{42});
    test_roundtrip(rpc::payload::HandshakeConfig{200, 5, 2000});
    test_roundtrip(rpc::payload::SetBaudratePacket{57600});
    test_roundtrip(rpc::payload::LinkSync{0xAA});
    test_roundtrip(rpc::payload::EnterBootloader{0xBB});
    
    rpc::payload::SpiTransfer st;
    st.data = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(st);
    
    rpc::payload::SpiTransferResponse strr;
    strr.data = etl::span<const uint8_t>((const uint8_t*)str, 4);
    test_roundtrip(strr);
    
    test_roundtrip(rpc::payload::SpiConfig{1, 2, 3});
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_all_structs_roundtrip);
    return UNITY_END();
}
