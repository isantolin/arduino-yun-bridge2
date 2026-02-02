/*
 * BridgeControl - Sketch Funcional con Password en Runtime
 * * Ahora el secreto se define AQUÍ en el sketch y se pasa a la librería.
 */
#define BRIDGE_ENABLE_DATASTORE 0
#define BRIDGE_ENABLE_FILESYSTEM 0
#define BRIDGE_ENABLE_PROCESS 0

// CONFIGURACIÓN DEL SECRETO
// Este password debe coincidir con el configurado en el lado de Linux (/etc/mcu-bridge.conf o similar)
#define BRIDGE_SECRET "12345678901234567890123456789012"

// Includes manuales para dependencias
// #include <PacketSerial.h> // Removed: Internal dependency
// #include <CRC32.h>        // Removed: Internal dependency
// #include <Crypto.h>       // Removed: Internal dependency

#include <Bridge.h>
#include <string.h>

void handleDigitalReadResponse(uint8_t value) {
  (void)value;
}

void handleCommand(const rpc::Frame& frame) {
  (void)frame;
}

void handleMailboxMessage(const uint8_t* buffer, uint16_t size) {
  char msg_buf[80];
  if (size < sizeof(msg_buf)) {
    memcpy(msg_buf, buffer, size);
    msg_buf[size] = '\0';

    if (strcmp(msg_buf, "ON") == 0) {
      digitalWrite(13, HIGH);
    } else if (strcmp(msg_buf, "OFF") == 0) {
      digitalWrite(13, LOW);
    }
  }
  Mailbox.requestRead();
}

void handleStatusFrame(rpc::StatusCode status_code, const uint8_t* payload, uint16_t length) {
  (void)payload;
  (void)length;
  (void)status_code;
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
  
  Mailbox.requestRead();
}

void loop() {
  Bridge.process();
}