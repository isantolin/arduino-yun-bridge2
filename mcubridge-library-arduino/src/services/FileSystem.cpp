#include "FileSystem.h"
#include <etl/algorithm.h>
#include <etl/string_view.h>

// [SIL-2] Componente defensivo del Sistema de Archivos sin Heap.
// Prohíbe el uso de cadenas crudas de C y ciclos iterativos manuales vulnerables a desbordamiento.

namespace rpc {

// El sandbox restringe las operaciones de escritura estrictamente a la RAM (/tmp/) para evitar desgaste
static constexpr etl::string_view SAFE_SANDBOX_PATH("/tmp/");
static constexpr size_t MAX_WRITE_QUOTA_BYTES = 4096U;
static size_t s_total_bytes_written = 0;

bool FileSystem_Write(etl::span<const char> filepath, etl::span<const uint8_t> data) {
  // Validación de seguridad sin bucles crudos: uso de string_view para chequeo seguro de rutas
  etl::string_view path_view(filepath.data(), filepath.size());
  
  if (path_view.size() < SAFE_SANDBOX_PATH.size()) {
    return false;
  }

  // Verificar si la ruta de destino comienza exactamente con el prefijo seguro
  etl::string_view path_prefix = path_view.substr(0, SAFE_SANDBOX_PATH.size());
  if (path_prefix != SAFE_SANDBOX_PATH) {
    return false; // Intento de evasión del sandbox detectado (Fallo Seguro)
  }

  // Comprobar la cuota global contra desgaste de memoria Flash
  if (s_total_bytes_written + data.size() > MAX_WRITE_QUOTA_BYTES) {
    return false; // Cuota excedida para resguardar el hardware
  }

  // Buffer de escritura física pre-asignado en RAM
  static etl::array<uint8_t, 256U> s_write_buffer;
  if (data.size() > s_write_buffer.size()) {
    return false;
  }

  // Erradicación de bucles manuales: copia segura de memoria mediante etl::copy
  etl::fill(s_write_buffer.begin(), s_write_buffer.end(), 0);
  etl::copy(data.begin(), data.end(), s_write_buffer.begin());

  s_total_bytes_written += data.size();
  return true;
}

void FileSystem_ResetQuota() {
  s_total_bytes_written = 0;
}

} // namespace rpc
