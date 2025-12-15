
#include "Bridge.h"
#include "arduino/StringUtils.h"
#include <string.h>
#include "protocol/rpc_protocol.h"

using namespace rpc;

DataStoreClass::DataStoreClass() {}

void DataStoreClass::put(const char* key, const char* value) {
  if (!key || !value) return;

  const auto key_info = measure_bounded_cstring(
      key, BridgeClass::kMaxDatastoreKeyLength);
  if (key_info.length == 0 || key_info.overflowed) {
    return;
  }

  const auto value_info = measure_bounded_cstring(
      value, BridgeClass::kMaxDatastoreKeyLength);
  if (value_info.overflowed) {
    return;
  }

  const size_t key_len = key_info.length;
  const size_t value_len = value_info.length;

  const size_t payload_len = 2 + key_len + value_len;
  if (payload_len > MAX_PAYLOAD_SIZE) return;

  // [OPTIMIZATION] Use shared scratch buffer instead of stack allocation
  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);
  payload[1 + key_len] = static_cast<uint8_t>(value_len);
  memcpy(payload + 2 + key_len, value, value_len);

  (void)Bridge.sendFrame(
      CommandId::CMD_DATASTORE_PUT,
      payload, static_cast<uint16_t>(payload_len));
}

void DataStoreClass::requestGet(const char* key) {
  if (!key) return;
  const auto key_info = measure_bounded_cstring(
      key, BridgeClass::kMaxDatastoreKeyLength);
  if (key_info.length == 0 || key_info.overflowed) return;
  const size_t key_len = key_info.length;

  // [OPTIMIZATION] Use shared scratch buffer
  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);

  if (!Bridge._trackPendingDatastoreKey(key)) {
    Bridge._emitStatus(StatusCode::STATUS_ERROR, "datastore_queue_full");
    return;
  }

  (void)Bridge.sendFrame(
      CommandId::CMD_DATASTORE_GET,
      payload, static_cast<uint16_t>(key_len + 1));
}
