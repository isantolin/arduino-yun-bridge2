#include <Bridge.h>
#include <rpc_protocol.h>

// Esta es nuestra función "callback". La librería Bridge la llamará
// automáticamente cada vez que reciba un comando válido desde Linux.
void handle_incoming_command(const rpc::Frame& frame) {
  Serial.println(">> Comando recibido por la librería Bridge!");
  Serial.print("Comando: 0x"); Serial.println(frame.header.command_id, HEX);

  switch (frame.header.command_id) {
    case rpc::CMD_DIGITAL_WRITE:
      if (frame.header.payload_length >= 2) {
        uint8_t pin = frame.payload[0];
        uint8_t value = frame.payload[1];
        pinMode(pin, OUTPUT); // Asegurarse de que el pin es de salida
        digitalWrite(pin, value);
        Serial.print("digitalWrite en pin "); Serial.print(pin);
        Serial.print(" valor "); Serial.println(value);
      }
      break;

    case rpc::CMD_ANALOG_WRITE:
      if (frame.header.payload_length >= 2) {
        uint8_t pin = frame.payload[0];
        uint8_t value = frame.payload[1];
        analogWrite(pin, value);
        Serial.print("analogWrite en pin "); Serial.print(pin);
        Serial.print(" valor "); Serial.println(value);
      }
      break;

    case rpc::CMD_DIGITAL_READ:
      if (frame.header.payload_length >= 1) {
        uint8_t pin = frame.payload[0];
        pinMode(pin, INPUT); // Asegurarse de que el pin es de entrada
        uint8_t value = digitalRead(pin);
        Serial.print("digitalRead en pin "); Serial.print(pin);
        Serial.print(" valor "); Serial.println(value);
        // Enviar respuesta a Linux
        uint8_t payload[2] = {pin, value};
        Bridge.sendFrame(rpc::CMD_DIGITAL_READ_RESP, payload, 2);
      }
      break;

    case rpc::CMD_ANALOG_READ:
      if (frame.header.payload_length >= 1) {
        uint8_t pin = frame.payload[0];
        int value = analogRead(pin);
        Serial.print("analogRead en pin "); Serial.print(pin);
        Serial.print(" valor "); Serial.println(value);
        // Enviar respuesta a Linux
        uint8_t payload[3] = {pin, (uint8_t)(value & 0xFF), (uint8_t)(value >> 8)};
        Bridge.sendFrame(rpc::CMD_ANALOG_READ_RESP, payload, 3);
      }
      break;

    case rpc::CMD_CONSOLE_WRITE:
      // Mensaje recibido desde Linux para ser impreso en la consola del Arduino.
      if (frame.header.payload_length > 0) {
        // Copiamos el payload a un buffer con terminación NULL para seguridad
        char buf[frame.header.payload_length + 1];
        memcpy(buf, frame.payload, frame.header.payload_length);
        buf[frame.header.payload_length] = '\0';
        Serial.print("(From Linux): ");
        Serial.println(buf);
      }
      break;

    default:
      // El resto de comandos (como las respuestas del Mailbox) son gestionados
      // internamente por la librería Bridge, por lo que no necesitan un case aquí.
      Serial.println("Comando no manejado por el sketch, podría ser interno de la librería.");
  }
}

void setup() {
  Serial.begin(115200); // Para debugging
  Bridge.begin(); // Inicia la librería Bridge (y el Serial1 a 115200)
  
  // Registramos nuestra función para que Bridge la llame con comandos de I/O.
  Bridge.onCommand(handle_incoming_command);
  
  Serial.println("[DEBUG] Setup completado. Bridge escuchando.");
  pinMode(13, OUTPUT);

  // Añadimos un delay para dar tiempo al lado Linux a arrancar completamente.
  // Esto previene una condición de carrera al iniciar la comunicación.
  Serial.println("[DEBUG] Esperando 10 segundos a que Linux inicie...");
  delay(10000);
  Serial.println("[DEBUG] Espera finalizada. Iniciando loop.");
}

void loop() {
  // Bridge.process() es esencial para la comunicación en segundo plano.
  Bridge.process();

  // Comprobamos si hay mensajes disponibles en el Mailbox antes de intentar leer.
  // Mailbox.available() pregunta al demonio de Linux si tiene mensajes en cola.
  if (Mailbox.available() > 0) {
    // Si hay mensajes, leemos uno. Esta llamada ahora es menos propensa a bloquearse
    // innecesariamente porque sabemos que hay un mensaje esperando.
    String msg = Mailbox.readString();

    if (msg.length() > 0) {
      Serial.print("Mensaje de Mailbox recibido: ");
      Serial.println(msg);

      // Ejemplo de uso: controlar el LED con mensajes "ON" / "OFF"
      if (msg == "ON") {
        digitalWrite(13, HIGH);
        Serial.println("LED 13 encendido por Mailbox");
      } else if (msg == "OFF") {
        digitalWrite(13, LOW);
        Serial.println("LED 13 apagado por Mailbox");
      }
      // --- NUEVO: Ejemplo para DataStore ---
      else if (msg.startsWith("put ")) { // Formato: "put clave=valor"
        String cmd = msg.substring(4);
        int separator = cmd.indexOf('=');
        if (separator > 0) {
          String key = cmd.substring(0, separator);
          String value = cmd.substring(separator + 1);
          DataStore.put(key, value);
          Serial.print("DataStore PUT: '");
          Serial.print(key);
          Serial.print("' = '");
          Serial.print(value);
          Serial.println("'");
        }
                } else if (msg.startsWith("get ")) { // Formato: "get clave"
                  String key = msg.substring(4);
                  String value = DataStore.get(key);
                  Serial.print("DataStore GET: '");
                  Serial.print(key);
                  Serial.print("' devolvió '");
                  Serial.print(value);
                  Serial.println("'");
                }
                // --- NUEVO: Ejemplo para FileIO ---
                else if (msg.startsWith("fwrite ")) { // Formato: "fwrite /tmp/filename.txt=content"
                  String cmd = msg.substring(7);
                  int separator = cmd.indexOf('=');
                  if (separator > 0) {
                    String filename = cmd.substring(0, separator);
                    String content = cmd.substring(separator + 1);

                    // Para escribir en un archivo en el lado de Linux, usamos el objeto FileSystem.
                    FileSystem.write(filename, content);
                    Serial.print("FileIO WRITE: Se escribio '");
                    Serial.print(content);
                    Serial.print("' en el archivo '");
                    Serial.print(filename);
                    Serial.println("'");
                  }
                } else if (msg.startsWith("fread ")) { // Formato: "fread /tmp/filename.txt"
                  String filename = msg.substring(6);

                  // Para leer un archivo del lado de Linux, usamos el objeto FileSystem.
                  String content = FileSystem.read(filename);

                  Serial.print("FileIO READ: Se leyo del archivo '");
                  Serial.print(filename);
                  Serial.print("': ");
                  Serial.println(content);
                }
              }  }
  
  // Esperamos un segundo antes de volver a comprobar para no saturar la comunicación.
  delay(1000);
}