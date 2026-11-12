#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <unity.h>

#include "pb_decode.h"
#include "pb_encode.h"
#include "protocol/mcu_wire.pb.h"
#include "protocol/rpc_protocol.h"

// Global stubs
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

template <typename T>
bool roundtrip_verify(const pb_msgdesc_t* fields, const T& p) {
  uint8_t buffer[rpc::MAX_PAYLOAD_SIZE];
  pb_ostream_t ostream = pb_ostream_from_buffer(buffer, sizeof(buffer));
  if (!pb_encode(&ostream, fields, &p)) return false;
  size_t used = ostream.bytes_written;

  T p2 = {};
  pb_istream_t istream = pb_istream_from_buffer(buffer, used);
  return pb_decode_noinit(&istream, fields, &p2);
}

void test_all_wire_messages_roundtrip() {
  size_t count = 0;

  {
    rpc_pb_VersionResponse p = rpc_pb_VersionResponse_init_default;
    p.major = 1;
    p.minor = 2;
    p.patch = 3;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_VersionResponse_fields, p));
    count++;
  }

  {
    rpc_pb_FreeMemoryResponse p = rpc_pb_FreeMemoryResponse_init_default;
    p.value = 1024;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_FreeMemoryResponse_fields, p));
    count++;
  }

  {
    rpc_pb_Capabilities p = rpc_pb_Capabilities_init_default;
    p.ver = 1;
    p.arch = 2;
    p.dig = 20;
    p.ana = 6;
    p.feat = 0xFF;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_Capabilities_fields, p));
    count++;
  }

  {
    rpc_pb_PinMode p = rpc_pb_PinMode_init_default;
    p.pin = 13;
    p.mode = rpc_pb_PinModeType_PIN_OUTPUT;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_PinMode_fields, p));
    count++;
  }

  {
    rpc_pb_DigitalWrite p = rpc_pb_DigitalWrite_init_default;
    p.pin = 13;
    p.value = 1;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_DigitalWrite_fields, p));
    count++;
  }

  {
    rpc_pb_AnalogWrite p = rpc_pb_AnalogWrite_init_default;
    p.pin = 9;
    p.value = 128;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_AnalogWrite_fields, p));
    count++;
  }

  {
    rpc_pb_PinRead p = rpc_pb_PinRead_init_default;
    p.pin = 2;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_PinRead_fields, p));
    count++;
  }

  {
    rpc_pb_ClockSyncRequest p = rpc_pb_ClockSyncRequest_init_default;
    p.host_time_us = 1234567890ULL;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_ClockSyncRequest_fields, p));
    count++;
  }

  {
    rpc_pb_ClockSyncResponse p = rpc_pb_ClockSyncResponse_init_default;
    p.host_time_us = 1234567890ULL;
    p.mcu_time_us = 12345U;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_ClockSyncResponse_fields, p));
    count++;
  }

  {
    rpc_pb_PinSubscribeRequest p = rpc_pb_PinSubscribeRequest_init_default;
    p.pin = 3;
    p.mode = rpc_pb_PinModeType_PIN_INPUT_PULLUP;
    p.interval_ms = 50;
    p.hysteresis = 2;
    p.enabled = true;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_PinSubscribeRequest_fields, p));
    count++;
  }

  {
    rpc_pb_PinSubscribeResponse p = rpc_pb_PinSubscribeResponse_init_default;
    p.pin = 3;
    p.success = true;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_PinSubscribeResponse_fields, p));
    count++;
  }

  {
    rpc_pb_PinUpdateEvent p = rpc_pb_PinUpdateEvent_init_default;
    p.pin = 3;
    p.value = 1;
    p.timestamp_micros = 50000;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_PinUpdateEvent_fields, p));
    count++;
  }

  {
    rpc_pb_DigitalReadResponse p = rpc_pb_DigitalReadResponse_init_default;
    p.value = 1;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_DigitalReadResponse_fields, p));
    count++;
  }

  {
    rpc_pb_AnalogReadResponse p = rpc_pb_AnalogReadResponse_init_default;
    p.value = 512;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_AnalogReadResponse_fields, p));
    count++;
  }

  {
    rpc_pb_ConsoleWrite p = rpc_pb_ConsoleWrite_init_default;
    p.data.size = 5;
    memcpy(p.data.bytes, "hello", 5);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_ConsoleWrite_fields, p));
    count++;
  }

  {
    rpc_pb_DatastorePut p = rpc_pb_DatastorePut_init_default;
    strncpy(p.key, "key1", sizeof(p.key) - 1);
    p.value.size = 4;
    memcpy(p.value.bytes, "val1", 4);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_DatastorePut_fields, p));
    count++;
  }

  {
    rpc_pb_DatastoreGet p = rpc_pb_DatastoreGet_init_default;
    strncpy(p.key, "key1", sizeof(p.key) - 1);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_DatastoreGet_fields, p));
    count++;
  }

  {
    rpc_pb_DatastoreGetResponse p = rpc_pb_DatastoreGetResponse_init_default;
    p.value.size = 4;
    memcpy(p.value.bytes, "val1", 4);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_DatastoreGetResponse_fields, p));
    count++;
  }

  {
    rpc_pb_MailboxPush p = rpc_pb_MailboxPush_init_default;
    p.data.size = 4;
    memcpy(p.data.bytes, "mail", 4);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_MailboxPush_fields, p));
    count++;
  }

  {
    rpc_pb_MailboxProcessed p = rpc_pb_MailboxProcessed_init_default;
    p.message_id = 99;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_MailboxProcessed_fields, p));
    count++;
  }

  {
    rpc_pb_MailboxAvailableResponse p =
        rpc_pb_MailboxAvailableResponse_init_default;
    p.count = 5;
    TEST_ASSERT_TRUE(
        roundtrip_verify(rpc_pb_MailboxAvailableResponse_fields, p));
    count++;
  }

  {
    rpc_pb_MailboxReadResponse p = rpc_pb_MailboxReadResponse_init_default;
    p.content.size = 4;
    memcpy(p.content.bytes, "data", 4);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_MailboxReadResponse_fields, p));
    count++;
  }

  {
    rpc_pb_FileWrite p = rpc_pb_FileWrite_init_default;
    strncpy(p.path, "/tmp/foo", sizeof(p.path) - 1);
    p.data.size = 4;
    memcpy(p.data.bytes, "1234", 4);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_FileWrite_fields, p));
    count++;
  }

  {
    rpc_pb_FileRemove p = rpc_pb_FileRemove_init_default;
    strncpy(p.path, "/tmp/foo", sizeof(p.path) - 1);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_FileRemove_fields, p));
    count++;
  }

  {
    rpc_pb_ProcessRunAsync p = rpc_pb_ProcessRunAsync_init_default;
    strncpy(p.command, "ls -la", sizeof(p.command) - 1);
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_ProcessRunAsync_fields, p));
    count++;
  }

  {
    rpc_pb_ProcessRunAsyncResponse p =
        rpc_pb_ProcessRunAsyncResponse_init_default;
    p.pid = 1234;
    TEST_ASSERT_TRUE(
        roundtrip_verify(rpc_pb_ProcessRunAsyncResponse_fields, p));
    count++;
  }

  {
    rpc_pb_ProcessPoll p = rpc_pb_ProcessPoll_init_default;
    p.pid = 1234;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_ProcessPoll_fields, p));
    count++;
  }

  {
    rpc_pb_ProcessPollResponse p = rpc_pb_ProcessPollResponse_init_default;
    p.status = 0;
    p.exit_code = 0;
    p.finished = true;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_ProcessPollResponse_fields, p));
    count++;
  }

  {
    rpc_pb_ProcessKill p = rpc_pb_ProcessKill_init_default;
    p.pid = 1234;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_ProcessKill_fields, p));
    count++;
  }

  {
    rpc_pb_SetBaudratePacket p = rpc_pb_SetBaudratePacket_init_default;
    p.baudrate = 115200;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_SetBaudratePacket_fields, p));
    count++;
  }

  {
    rpc_pb_EnterBootloader p = rpc_pb_EnterBootloader_init_default;
    p.magic = 0xDEADBEEF;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_EnterBootloader_fields, p));
    count++;
  }

  {
    rpc_pb_SpiTransfer p = rpc_pb_SpiTransfer_init_default;
    p.data.size = 2;
    p.data.bytes[0] = 0x55;
    p.data.bytes[1] = 0xAA;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_SpiTransfer_fields, p));
    count++;
  }

  {
    rpc_pb_SpiTransferResponse p = rpc_pb_SpiTransferResponse_init_default;
    p.data.size = 2;
    p.data.bytes[0] = 0x12;
    p.data.bytes[1] = 0x34;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_SpiTransferResponse_fields, p));
    count++;
  }

  {
    rpc_pb_SpiConfig p = rpc_pb_SpiConfig_init_default;
    p.bit_order = rpc_pb_SpiBitOrder_SPI_BIT_ORDER_MSB_FIRST;
    p.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_0;
    p.frequency = 4000000;
    TEST_ASSERT_TRUE(roundtrip_verify(rpc_pb_SpiConfig_fields, p));
    count++;
  }

  TEST_ASSERT_EQUAL(36, count);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_all_wire_messages_roundtrip);
  return UNITY_END();
}
