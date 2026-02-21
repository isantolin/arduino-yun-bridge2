/*
 * BridgeControl - Sketch Funcional con Password en Runtime
 * * Ahora el secreto se define AQUÍ en el sketch y se pasa a la librería.
 */

// CONFIGURACIÓN DEL SECRETO
// Este password debe coincidir con el configurado en el lado de Linux (/etc/mcu-bridge.conf o similar)
#define BRIDGE_SECRET "12345678901234567890123456789012"

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
  // AHORA PASAMOS EL SECRETO AQUÍ
  // Argumento 1: Baudrate (por defecto 115200)
  // Argumento 2: El secreto compartido
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SECRET);

  Bridge.onDigitalReadResponse(BridgeClass::DigitalReadHandler::create<handleDigitalReadResponse>());
  Bridge.onCommand(BridgeClass::CommandHandler::create<handleCommand>());
  Mailbox.onMailboxMessage(MailboxClass::MailboxHandler::create<handleMailboxMessage>());
  Bridge.onStatus(BridgeClass::StatusHandler::create<handleStatusFrame>());
  
  pinMode(13, OUTPUT);

  // Espera no bloqueante para sincronización
  unsigned long lastBlink = 0;
  bool ledState = false;
  
  // Nota: En sistemas reales, loop() se encargará de process(),
  // pero aquí bloqueamos el setup() intencionalmente hasta sincronizar
  // para asegurar estado conocido, pero SIN delay().
  while (!Bridge.isSynchronized()) {
    Bridge.process();
    if (millis() - lastBlink > 100) {
      lastBlink = millis();
      ledState = !ledState;
      digitalWrite(13, ledState ? HIGH : LOW);
    }
  }
  
  Console.begin();
  Console.println(F("Bridge iniciado con secreto definido en Sketch."));
}

void loop() {
  Bridge.process();
  
  // [ANTI-FLOOD] Poll mailbox every 500ms instead of continuous loop
  static unsigned long lastMailboxCheck = 0;
  if (millis() - lastMailboxCheck > 500) {
    lastMailboxCheck = millis();
    Mailbox.requestRead();
  }
}
