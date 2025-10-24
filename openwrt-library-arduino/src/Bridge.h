/**
 * @file Bridge.h
 * @brief Librería principal del Arduino Yun Bridge v2.
 * @details Esta librería facilita la comunicación RPC (Remote Procedure Call)
 * entre el microcontrolador Arduino y el procesador Linux en placas como
 * el Arduino Yún.
 *
 * Copyright (C) 2025 Ignacio Santolin and contributors
 * Licenciado bajo la GNU General Public License, v3 o posterior.
 */
#ifndef BRIDGE_V2_H
#define BRIDGE_V2_H

#include <Arduino.h>

#include "Print.h"
#include "rpc_frame.h"

// --- Constantes de la Consola ---
#define CONSOLE_RX_BUFFER_SIZE 64
#define CONSOLE_BUFFER_HIGH_WATER 50
#define CONSOLE_BUFFER_LOW_WATER 10

/**
 * @class ConsoleClass
 * @brief Permite enviar y recibir datos de texto a/desde la consola de Linux.
 * @details Se comporta de manera similar al objeto `Serial`, pero la comunicación
 * se redirige a través del Bridge hacia el procesador Linux.
 */
class ConsoleClass : public Print {
 public:
  ConsoleClass();

  /** @brief Inicializa la consola. Llamado automáticamente por Bridge.begin(). */
  void begin();

  /**
   * @brief Escribe un solo byte en la consola de Linux.
   * @param c El byte a escribir.
   * @return El número de bytes escritos.
   */
  virtual size_t write(uint8_t c);

  /**
   * @brief Escribe un buffer de bytes en la consola de Linux.
   * @param buffer Puntero al buffer de datos.
   * @param size Número de bytes a escribir.
   * @return El número de bytes escritos.
   */
  virtual size_t write(const uint8_t* buffer, size_t size);

  /**
   * @brief Devuelve el número de bytes disponibles para leer desde la consola de Linux.
   * @return El número de bytes en el buffer de recepción.
   */
  int available();

  /**
   * @brief Lee el siguiente byte del buffer de recepción.
   * @return El primer byte de datos entrantes disponibles (o -1 si no hay datos).
   */
  int read();

  /**
   * @brief Devuelve el siguiente byte del buffer sin eliminarlo.
   * @return El primer byte de datos (o -1 si no hay datos).
   */
  int peek();

  /** @brief Espera a que se complete la transmisión de datos salientes. (No implementado) */
  void flush();

  /** @brief Permite comprobar si la consola ha sido inicializada. ej: `if (Console) { ... }` */
  explicit operator bool() const { return _begun; }

  // --- Métodos internos ---
  /**
   * @brief (Uso interno) Añade datos al buffer de recepción de la consola.
   * @param buffer Puntero a los datos recibidos.
   * @param size Tamaño de los datos.
   */
  void _push(const uint8_t* buffer, size_t size);

 private:
  bool _begun;
  uint8_t _rx_buffer[CONSOLE_RX_BUFFER_SIZE];
  volatile uint16_t _rx_buffer_head;
  volatile uint16_t _rx_buffer_tail;
  bool _xoff_sent;
};

/**
 * @class DataStoreClass
 * @brief Proporciona un almacén de clave-valor persistente en el lado de Linux.
 * @details Permite al sketch de Arduino guardar y recuperar datos que persisten
 * entre reinicios del microcontrolador.
 */
class DataStoreClass {
 public:
  DataStoreClass();

  /**
   * @brief Guarda un par clave-valor en el almacén de datos de Linux.
   * @param key La clave como una cadena de caracteres C.
   * @param value El valor como una cadena de caracteres C.
   */
  void put(const char* key, const char* value);

  /**
   * @brief Recupera un valor del almacén de datos de Linux.
   * @param key La clave cuyo valor se quiere obtener.
   * @param buffer Un buffer pre-alocado para guardar el valor.
   * @param length El tamaño del buffer.
   * @return El número de bytes leídos, o -1 en caso de error/timeout.
   */
  int get(const char* key, char* buffer, size_t length);

 private:
  friend class BridgeClass;
};

/**
 * @class MailboxClass
 * @brief Permite el intercambio de mensajes entre Arduino y Linux.
 * @details Los mensajes son strings o buffers de bytes.
 */
class MailboxClass {
 public:
  MailboxClass();
  void begin();

  /**
   * @brief Envía un mensaje de tipo String a Linux.
   * @param message El mensaje a enviar.
   */
  void send(const String& message);

  /**
   * @brief Envía un buffer de datos a Linux.
   * @param data Puntero al buffer de datos.
   * @param length Longitud de los datos.
   */
  void send(const uint8_t* data, size_t length);

  /**
   * @brief Comprueba si hay mensajes de Linux esperando en el buzón.
   * @return El número de mensajes disponibles.
   */
  int available();

  /**
   * @brief Lee un mensaje del buzón en un buffer proporcionado.
   * @param buffer Puntero al buffer donde se guardará el mensaje.
   * @param length Tamaño máximo del buffer.
   * @return El número de bytes leídos, o -1 si no hay mensaje.
   */
  int read(uint8_t* buffer, size_t length);

  /**
   * @brief Lee un mensaje del buzón y lo devuelve como un String.
   * @return El mensaje recibido.
   */
  String readString();

 private:
  uint8_t _buffer[256];
  size_t _length;
  friend class BridgeClass;
};

/**
 * @class FileSystemClass
 * @brief Permite al sketch de Arduino interactuar con el sistema de ficheros de Linux.
 */
class FileSystemClass {
 public:
  void begin();

  /**
   * @brief Escribe datos de un String en un fichero en Linux. Sobrescribe el fichero si existe.
   * @param filePath Ruta completa del fichero en el sistema de Linux.
   * @param data El contenido a escribir.
   */
  void write(const String& filePath, const String& data);

  /**
   * @brief Escribe datos de un buffer en un fichero en Linux. Sobrescribe el fichero si existe.
   * @param filePath Ruta completa del fichero.
   * @param data Puntero al buffer de datos.
   * @param length Número de bytes a escribir.
   */
  void write(const String& filePath, const uint8_t* data, size_t length);

  /**
   * @brief Lee el contenido completo de un fichero de Linux.
   * @param filePath Ruta completa del fichero.
   * @return Un String con el contenido del fichero.
   */
  String read(const String& filePath);

  /**
   * @brief Lee el contenido de un fichero en un buffer de caracteres.
   * @param filePath Ruta completa del fichero.
   * @param buffer Buffer donde se guardará el contenido.
   * @param length Tamaño del buffer.
   * @return El número de bytes leídos.
   */
  int read(const String& filePath, char* buffer, size_t length);

  /**
   * @brief Elimina un fichero del sistema de Linux.
   * @param filePath Ruta completa del fichero a eliminar.
   */
  void remove(const String& filePath);
};

/**
 * @class ProcessClass
 * @brief Permite al sketch de Arduino ejecutar comandos y procesos en Linux.
 */
class ProcessClass {
 public:
  ProcessClass();

  /**
   * @brief Ejecuta un comando en Linux de forma síncrona.
   * @details El sketch se bloqueará hasta que el comando termine.
   * @param command El comando a ejecutar.
   * @return La salida estándar (stdout) del comando.
   */
  String run(const String& command);

  /**
   * @brief Ejecuta un comando en Linux de forma asíncrona.
   * @details El comando se ejecuta en segundo plano y el sketch no se bloquea.
   * @param command El comando a ejecutar.
   * @return Un ID de proceso (pid) para poder consultar su estado más tarde.
   */
  int runAsynchronously(const String& command);

  /**
   * @brief Consulta la salida de un proceso asíncrono.
   * @param pid El ID del proceso devuelto por `runAsynchronously`.
   * @return La salida estándar (stdout) acumulada del proceso hasta el momento.
   */
  String poll(int pid);

  /**
   * @brief Termina un proceso que se está ejecutando en Linux.
   * @param pid El ID del proceso a terminar.
   */
  void kill(int pid);
};

/**
 * @class BridgeClass
 * @brief Clase principal que gestiona la comunicación RPC.
 */
class BridgeClass {
 public:
  /**
   * @brief Constructor de la clase Bridge.
   * @param stream El stream de comunicación (normalmente Serial1 en el Yún).
   */
  BridgeClass(Stream& stream);

  /**
   * @brief Inicializa la comunicación del Bridge. Debe llamarse en `setup()`.
   */
  void begin();

  /**
   * @brief Procesa los datos entrantes. Debe llamarse en cada iteración de `loop()`.
   */
  void process();

  // --- Manejadores de Comandos y Respuestas (Callbacks) ---

  /** @brief (Avanzado) Define un manejador para comandos RPC personalizados. */
  typedef void (*CommandHandler)(const rpc::Frame& frame);
  void onCommand(CommandHandler handler);

  /** @brief Define un manejador para respuestas de lectura digital. */
  typedef void (*DigitalReadHandler)(uint8_t pin, int value);
  void onDigitalReadResponse(DigitalReadHandler handler);

  /** @brief Define un manejador para respuestas de lectura analógica. */
  typedef void (*AnalogReadHandler)(uint8_t pin, int value);
  void onAnalogReadResponse(AnalogReadHandler handler);

  // --- API de Control de Pines (No Bloqueante) ---

  /** @brief Configura el modo de un pin (INPUT, OUTPUT, INPUT_PULLUP). */
  void pinMode(uint8_t pin, uint8_t mode);

  /** @brief Escribe un valor digital en un pin. */
  void digitalWrite(uint8_t pin, uint8_t value);

  /** @brief Escribe un valor analógico (PWM) en un pin. */
  void analogWrite(uint8_t pin, int value);

  /** @brief Solicita el valor de un pin digital de forma asíncrona. La respuesta llegará al callback registrado. */
  void requestDigitalRead(uint8_t pin);

  /** @brief Solicita el valor de un pin analógico de forma asíncrona. La respuesta llegará al callback registrado. */
  void requestAnalogRead(uint8_t pin);

  // --- Métodos Obsoletos (Bloqueantes) ---
  // Se mantienen por compatibilidad pero su uso no es recomendado.
  // Prefiera la API asíncrona con callbacks.
  int digitalRead(uint8_t pin) __attribute__((deprecated("Use requestDigitalRead and a callback instead")));
  int analogRead(uint8_t pin) __attribute__((deprecated("Use requestAnalogRead and a callback instead")));


  // --- Métodos Internos y de Bajo Nivel ---
  void sendFrame(uint16_t command_id, const uint8_t* payload,
                 uint16_t payload_len);

  // --- Funcionalidad Síncrona Obsoleta (para uso interno de la librería) ---
  // Estas funciones se mueven a 'public' para que DataStore, Mailbox, etc.,
  // puedan usarlas.
  String waitForResponse(uint16_t command, const uint8_t* payload,
                         uint16_t payload_len, unsigned long timeout = 1000);
  int waitForResponseAsInt(uint16_t command, const uint8_t* payload,
                           uint16_t payload_len,
                           unsigned long timeout = 1000);
  int waitForResponse(uint16_t command, const uint8_t* payload,
                    uint16_t payload_len, char* buffer,
                    size_t buffer_len, unsigned long timeout = 1000);
  
  // --- Agregadas las sobrecargas que faltaban ---
  String waitForResponse(uint16_t command, unsigned long timeout = 1000);
  int waitForResponseAsInt(uint16_t command, unsigned long timeout = 1000);


 private:
  void setBaudRate(long baud);
  Stream& _stream;
  rpc::FrameParser _parser;
  rpc::FrameBuilder _builder;

  // Punteros a las funciones de callback
  CommandHandler _command_handler;
  DigitalReadHandler _digital_read_handler;
  AnalogReadHandler _analog_read_handler;

  void dispatch(const rpc::Frame& frame);

  // --- Funcionalidad Síncrona Obsoleta ---
  // Las variables de estado SÍ deben ser privadas.
  volatile bool _response_received;
  uint16_t _waiting_for_cmd;
  uint8_t _response_payload[256];
  uint16_t _response_len;
};

// --- Instancias Globales ---
/// Objeto principal del Bridge. Usar para inicializar y procesar la comunicación.
extern BridgeClass Bridge;
/// Objeto para la comunicación con la consola de Linux.
extern ConsoleClass Console;
/// Objeto para el almacén de clave-valor.
extern DataStoreClass DataStore;
/// Objeto para el intercambio de mensajes.
extern MailboxClass Mailbox;
/// Objeto para la gestión de ficheros en Linux.
extern FileSystemClass FileSystem;
/// Objeto para la ejecución de procesos en Linux.
extern ProcessClass Process;

#endif  // BRIDGE_V2_H
