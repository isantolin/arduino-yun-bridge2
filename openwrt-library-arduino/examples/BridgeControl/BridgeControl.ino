/*
 * BridgeControl - Sketch Funcional con Password en Runtime
 * * Ahora el secreto se define AQUÍ en el sketch y se pasa a la librería.
 */
#define BRIDGE_ENABLE_DATASTORE 0
#define BRIDGE_ENABLE_FILESYSTEM 0
#define BRIDGE_ENABLE_PROCESS 0

// CONFIGURACIÓN DEL SECRETO
// Este password debe coincidir con el configurado en el lado de Linux (/etc/yun-bridge.conf o similar)
#define BRIDGE_SECRET "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"

// Includes manuales para dependencias
// #include <PacketSerial.h> // Removed: Internal dependency
// #include <CRC32.h>        // Removed: Internal dependency
// #include <Crypto.h>       // Removed: Internal dependency

#include <Bridge.h>
#include <string.h>

void printHexValue(Print& target, uint16_t value, uint8_t width) {
  if (width == 0) {
    return;
  }
  if (width > 4) {
    width = 4;
  }
  char buffer[4];
  for (int i = width - 1; i >= 0; --i) {
    buffer[i] = ("0123456789ABCDEF")[value & 0x0Fu];
    value >>= 4;
  }
  for (uint8_t i = 0; i < width; ++i) {
    target.print(buffer[i]);
  }
}

void handleDigitalReadResponse(uint8_t value) {
  Console.print("Respuesta asíncrona de lectura digital: ");
  Console.println(value);
}

void handleCommand(const rpc::Frame& frame) {
  Console.print("Comando RPC no manejado: ID=0x");
  printHexValue(Console, frame.header.command_id, 4);
  Console.print(", Payload Len=");
  Console.println(frame.header.payload_length);
}

void handleMailboxMessage(const uint8_t* buffer, uint16_t size) {
  char msg_buf[80];
  if (size < sizeof(msg_buf)) {
    memcpy(msg_buf, buffer, size);
    msg_buf[size] = '\0';

    Console.print("Mensaje de Mailbox recibido: ");
    Console.println(msg_buf);

    if (strcmp(msg_buf, "ON") == 0) {
      digitalWrite(13, HIGH);
      Console.println("LED 13 encendido por Mailbox");
    } else if (strcmp(msg_buf, "OFF") == 0) {
      digitalWrite(13, LOW);
      Console.println("LED 13 apagado por Mailbox");
    } else {
      Console.print("Comando desconocido: ");
      Console.println(msg_buf);
    }
  }
  Mailbox.requestRead();
}

void handleStatusFrame(rpc::StatusCode status_code, const uint8_t* payload, uint16_t length) {
  (void)payload;
  (void)length;
  // Silenced: printing on every status frame causes serial collisions
  // Only uncomment for debugging specific issues
  (void)status_code;
  /*
  Console.print("Estado: 0x");
  printHexValue(Console, rpc::to_underlying(status_code), 2);
  Console.println();
  */
}

void setup() {
  // AHORA PASAMOS EL SECRETO AQUÍ
  // Argumento 1: Baudrate (por defecto 115200)
  // Argumento 2: El secreto compartido
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SECRET);

  Bridge.onDigitalReadResponse(handleDigitalReadResponse);
  Bridge.onCommand(handleCommand);
  Mailbox.onMailboxMessage(handleMailboxMessage);
  Bridge.onStatus(handleStatusFrame);
  
  pinMode(13, OUTPUT);
  // delay(2000); // Removed blocking delay

  // Wait for handshake with non-blocking LED blink
  long lastBlink = 0;
  bool ledState = false;
  while (!Bridge.isSynchronized()) {
    Bridge.process();
    if (millis() - lastBlink > 50) {
      lastBlink = millis();
      ledState = !ledState;
      digitalWrite(13, ledState ? HIGH : LOW);
    }
  }
  
  Console.println("Bridge iniciado con secreto definido en Sketch.");
  Mailbox.requestRead();
}

void loop() {
  Bridge.process();
}