#include "DataStore.h"
#include <etl/algorithm.h>
#include <etl/vector.h>
#include <etl/string_view.h>

// [SIL-2] Servicio optimizado de almacenamiento clave-valor sin Heap.
// Utiliza algoritmos funcionales de ETL en lugar de bucles iterativos manuales.

namespace rpc {

struct DataEntry {
  etl::array<char, 32U> key;
  etl::array<char, 64U> value;
  bool active;
};

// Almacenamiento local pre-asignado estáticamente en Flash/RAM para evitar fragmentación.
static etl::vector<DataEntry, 16U> s_datastore;

bool DataStore_Get(etl::span<const char> key, etl::span<char> out_value, size_t& out_len) {
  // Erradicación de bucles manuales: uso de etl::find_if para búsquedas seguras con predicados
  auto it = etl::find_if(s_datastore.begin(), s_datastore.end(), [&key](const DataEntry& entry) {
    if (!entry.active) return false;
    // Comparación segura utilizando string_view de ETL
    etl::string_view entry_key(entry.key.data(), entry.key.size());
    etl::string_view search_key(key.data(), key.size());
    return entry_key == search_key;
  });

  if (it != s_datastore.end() && out_value.size() >= it->value.size()) {
    etl::copy(it->value.begin(), it->value.end(), out_value.begin());
    out_len = it->value.size();
    return true;
  }
  return false;
}

bool DataStore_Put(etl::span<const char> key, etl::span<const char> value) {
  if (key.size() > 32U || value.size() > 64U) {
    return false;
  }

  // Buscar si la clave ya existe para actualizarla
  auto it = etl::find_if(s_datastore.begin(), s_datastore.end(), [&key](const DataEntry& entry) {
    if (!entry.active) return false;
    etl::string_view entry_key(entry.key.data(), entry.key.size());
    etl::string_view search_key(key.data(), key.size());
    return entry_key == search_key;
  });

  if (it != s_datastore.end()) {
    etl::fill(it->value.begin(), it->value.end(), 0);
    etl::copy(value.begin(), value.end(), it->value.begin());
    return true;
  }

  // Si no existe, insertar un nuevo registro si hay capacidad en el vector pre-asignado
  if (s_datastore.full()) {
    return false;
  }

  DataEntry new_entry;
  etl::fill(new_entry.key.begin(), new_entry.key.end(), 0);
  etl::fill(new_entry.value.begin(), new_entry.value.end(), 0);
  etl::copy(key.begin(), key.end(), new_entry.key.begin());
  etl::copy(value.begin(), value.end(), new_entry.value.begin());
  new_entry.active = true;

  s_datastore.push_back(new_entry);
  return true;
}

void DataStore_Clear() {
  s_datastore.clear();
}

} // namespace rpc
