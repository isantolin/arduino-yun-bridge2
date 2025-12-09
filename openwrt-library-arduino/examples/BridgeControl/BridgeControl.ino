/*
 * BridgeControl - Sketch Funcional con Password en Runtime
 * * Ahora el secreto se define AQUÍ en el sketch y se pasa a la librería.
 */
#define BRIDGE_ENABLE_DATASTORE 0
#define BRIDGE_ENABLE_FILESYSTEM 0
#define BRIDGE_ENABLE_PROCESS 0

// CONFIGURACIÓN DEL SECRETO
// Este password debe coincidir con el configurado en el lado de Linux (/etc/yun-bridge.conf o similar)
#define BRIDGE_SECRET "changeme123"

// Includes manuales para dependencias
#include <PacketSerial.h>
#include <CRC32.h>
#include <Crypto.h>

#include <Bridge.h>
#include <string.h>

const int ledPin = 13;

void printHexValue(Print& target, uint16_t value, uint8_t width) {
  static constexpr char kHexDigits[] = "0123456789ABCDEF";
  if (width == 0) {
    return;
  }
  if (width > 4) {
    width = 4;
  }
  char buffer[4];
  for (int i = width - 1; i >= 0; --i) {
    buffer[i] = kHexDigits[value & 0x0F];
    value >>= 4;
  }
  for (uint8_t i = 0; i < width; ++i) {
    target.print(buffer[i]);
  }
}

void handleDigitalReadResponse(int value) {
  Console.print(F("Respuesta asíncrona de lectura digital: "));
  Console.println(value);
}

void handleCommand(const rpc::Frame& frame) {
  Console.print(F("Comando RPC no manejado: ID=0x"));
  printHexValue(Console, frame.header.command_id, 4);
  Console.print(F(", Payload Len="));
  Console.println(frame.header.payload_length);
}

void handleMailboxMessage(const uint8_t* buffer, size_t size) {
  char msg_buf[80];
  if (size < sizeof(msg_buf)) {
    memcpy(msg_buf, buffer, size);
    msg_buf[size] = '\0';

    Console.print(F("Mensaje de Mailbox recibido: "));
    Console.println(msg_buf);

    if (strcmp(msg_buf, "ON") == 0) {
      digitalWrite(ledPin, HIGH);
      Console.println(F("LED 13 encendido por Mailbox"));
    } else if (strcmp(msg_buf, "OFF") == 0) {
      digitalWrite(ledPin, LOW);
      Console.println(F("LED 13 apagado por Mailbox"));
    } else {
      Console.print(F("Comando desconocido: "));
      Console.println(msg_buf);
    }
  }
  Mailbox.requestRead();
}

void handleStatusFrame(rpc::StatusCode status_code, const uint8_t* payload, uint16_t length) {
  Console.print(F("Estado: 0x"));
  printHexValue(Console, rpc::to_underlying(status_code), 2);
  Console.println();
}

void setup() {
  // AHORA PASAMOS EL SECRETO AQUÍ
  // Argumento 1: Baudrate (por defecto 115200)
  // Argumento 2: El secreto compartido
  Bridge.begin(115200, BRIDGE_SECRET);

  Bridge.onDigitalReadResponse(handleDigitalReadResponse);
  Bridge.onCommand(handleCommand);
  Bridge.onMailboxMessage(handleMailboxMessage);
  Bridge.onStatus(handleStatusFrame);
  
  pinMode(ledPin, OUTPUT);
  delay(2000);
  
  Console.println(F("Bridge iniciado con secreto definido en Sketch."));
  Mailbox.requestRead();
}

void loop() {
  Bridge.process();
}