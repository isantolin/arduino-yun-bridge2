/*
 * BridgeControl - Sketch Funcional con Password en Runtime
 * * Ahora el secreto se define AQUÍ en el sketch y se pasa a la librería.
 */

// CONFIGURACIÓN DEL SECRETO
// Este password debe coincidir con el configurado en el lado de Linux (/etc/mcu-bridge.conf o similar)
#define BRIDGE_SECRET "DEBUG_INSECURE"

#include <Bridge.h>
#include <string.h>

void handleDigitalReadResponse(uint8_t value) {
  Console.print(F("Respuesta asíncrona de lectura digital: "));
  Console.println(value);
}

void handleCommand(const rpc::Frame& frame) {
  Console.print(F("Comando RPC no manejado: ID=0x"));
  Console.println(frame.header.command_id, HEX);
}

void handleMailboxMessage(const uint8_t* buffer, uint16_t size) {
  char msg_buf[80];
  if (size < sizeof(msg_buf)) {
    memcpy(msg_buf, buffer, size);
    msg_buf[size] = '\0';

    Console.print(F("Mensaje de Mailbox recibido: "));
    Console.println(msg_buf);

    if (strcmp(msg_buf, "ON") == 0) {
      digitalWrite(13, HIGH);
      Console.println(F("LED 13 encendido por Mailbox"));
    } else if (strcmp(msg_buf, "OFF") == 0) {
      digitalWrite(13, LOW);
      Console.println(F("LED 13 apagado por Mailbox"));
    } else {
      Console.print(F("Comando desconocido: "));
      Console.println(msg_buf);
    }
  }
}

void handleStatusFrame(rpc::StatusCode status_code, const uint8_t* payload, uint16_t length) {
  (void)payload;
  (void)length;
  // Solo imprimir errores graves para evitar saturación
  if (status_code != rpc::StatusCode::STATUS_OK) {
    Console.print(F("Error de Estado: 0x"));
    Console.println(static_cast<uint8_t>(status_code), HEX);
  }
}

void setup() {
  // [SIL-2] PROHIBIDO usar Serial.print() si comparte puerto con el Bridge.
  // En emulación, Serial (UART0) es el canal del protocolo. Cualquier texto
  // enviado aquí corromperá el stream COBS y bloqueará la sincronización.
  
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SECRET);

  Bridge.onDigitalReadResponse(BridgeClass::DigitalReadHandler::create<handleDigitalReadResponse>());
  Bridge.onCommand(BridgeClass::CommandHandler::create<handleCommand>());
  Mailbox.onMailboxMessage(MailboxClass::MailboxHandler::create<handleMailboxMessage>());
  Bridge.onStatus(BridgeClass::StatusHandler::create<handleStatusFrame>());
  
  pinMode(13, OUTPUT);

  // Bloqueo controlado hasta sincronizar SIN LOGS a Serial.
  while (!Bridge.isSynchronized()) {
    Bridge.process();
    // Podemos usar el LED para feedback visual si fuera hardware real
  }
  
  // Una vez sincronizado, Console es seguro porque viaja dentro de marcos RPC.
  Console.begin();
  Console.println(F("Bridge sincronizado y operando."));
}

void loop() {
  Bridge.process();
  
  static unsigned long lastMailboxCheck = 0;
  if (millis() - lastMailboxCheck > 500) {
    lastMailboxCheck = millis();
    Mailbox.requestRead();
  }
}
