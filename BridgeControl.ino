// BridgeControl.ino - Refactored for Robustness and Efficiency
// Generic sketch for controlling any pin via Bridge (Serial1)
// Uses strtok_r for safe command parsing and char arrays to avoid memory fragmentation.

#include <Bridge.h>

// Detect board type and set MAX_PINS accordingly
#if defined(ARDUINO_AVR_YUN) || defined(ARDUINO_AVR_UNO) || defined(ARDUINO_AVR_LEONARDO)
  #define MAX_PINS 20   // Yun/Uno/Leonardo: 0-19
#elif defined(ARDUINO_AVR_MEGA2560)
  #define MAX_PINS 54   // Mega: 0-53
#else
  #define MAX_PINS 20   // Default/fallback
#endif

// --- Globals ---
int pinStates[MAX_PINS];
const byte CMD_BUFFER_SIZE = 64;
char command_buffer[CMD_BUFFER_SIZE];
bool new_data = false;

// --- Core Pin Functions ---
void setPin(int pin, bool state) {
  if (pin < 0 || pin >= MAX_PINS) return;
  pinMode(pin, OUTPUT);
  digitalWrite(pin, state ? HIGH : LOW);
  pinStates[pin] = state ? HIGH : LOW;
}

void reportPinState(int pin) {
  if (pin < 0 || pin >= MAX_PINS) {
    Serial1.println("ERR Invalid pin");
    return;
  }
  Serial1.print("PIN");
  Serial1.print(pin);
  Serial1.print(" STATE ");
  Serial1.println(digitalRead(pin) == HIGH ? "ON" : "OFF");
}

// --- Command Handlers ---
void handlePinCommand(char* args) {
  char* pinStr = strtok_r(NULL, " ", &args);
  char* actionStr = strtok_r(NULL, " ", &args);

  if (!pinStr || !actionStr) {
    Serial1.println("ERR PIN command requires pin and action");
    return;
  }

  int pin = atoi(pinStr);
  if (pin < 0 || pin >= MAX_PINS) {
    Serial1.println("ERR Invalid pin number");
    return;
  }

  if (strcmp(actionStr, "ON") == 0) {
    setPin(pin, true);
    reportPinState(pin);
  } else if (strcmp(actionStr, "OFF") == 0) {
    setPin(pin, false);
    reportPinState(pin);
  } else if (strcmp(actionStr, "STATE") == 0) {
    reportPinState(pin);
  } else {
    Serial1.println("ERR Invalid pin action");
  }
}

void handleConsoleCommand(char* args) {
  if (args) {
    Serial.print("[CONSOLE] ");
    Serial.println(args);
    Serial1.println("OK CONSOLE");
  } else {
    Serial1.println("ERR CONSOLE message empty");
  }
}

// --- Command Parser ---
void parseCommand(char* command) {
  Serial.print("[DEBUG] Parsing command: ");
  Serial.println(command);

  // Create a mutable copy for strtok_r, as it modifies the string
  char command_copy[CMD_BUFFER_SIZE];
  strncpy(command_copy, command, CMD_BUFFER_SIZE);
  command_copy[CMD_BUFFER_SIZE - 1] = '\0';

  char* context; // For strtok_r
  char* commandToken = strtok_r(command_copy, " ", &context);

  if (!commandToken) return;

  // --- Local Commands (Executed on Arduino) ---
  if (strcmp(commandToken, "PIN") == 0) {
    handlePinCommand(context);
  } 
  else if (strcmp(commandToken, "CONSOLE") == 0) {
    handleConsoleCommand(context);
  }
  // --- All other commands are ignored by the Arduino ---
  // They are responses/confirmations from the daemon (e.g., "OK SET", "VALUE ...")
  // or unknown commands. We just print them for debugging but don't respond,
  // which prevents feedback loops.
  else {
    Serial.print("[DEBUG] Ignoring command/response from Linux: ");
    Serial.println(command);
  }
}

// --- Serial Communication ---
void readSerialCommand() {
  static byte ndx = 0;
  char endMarker = '\n';
  char rc;

  while (Serial1.available() > 0 && !new_data) {
    rc = Serial1.read();

    if (rc != endMarker) {
      if (ndx < CMD_BUFFER_SIZE - 1) {
        command_buffer[ndx] = rc;
        ndx++;
      }
    } else {
      command_buffer[ndx] = '\0'; // Null-terminate the string
      ndx = 0;
      new_data = true;
    }
  }
}

// --- Arduino Setup and Loop ---
void setup() {
  Bridge.begin();
  Serial.begin(115200);
  while (!Serial); // Wait for Serial Monitor to connect
  
  Serial.println("BridgeControl Sketch Ready.");
  
  for (int i = 0; i < MAX_PINS; i++) {
    pinStates[i] = LOW;
  }
  pinMode(13, OUTPUT); // Ensure LED_BUILTIN is ready
  pinStates[13] = LOW;
}

void loop() {
  readSerialCommand();

  if (new_data) {
    parseCommand(command_buffer);
    new_data = false;
  }

  // Non-blocking debug message to show the loop is active
  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 5000) {
    Serial.println("[DEBUG] Loop active");
    lastPrint = millis();
  }
}
