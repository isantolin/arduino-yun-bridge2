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

char serial1_cmd_buffer[CMD_BUFFER_SIZE]; // Buffer for commands from Linux (Serial1)
byte serial1_cmd_pos = 0;

char serial_cmd_buffer[CMD_BUFFER_SIZE];  // Buffer for commands from USB (Serial)
byte serial_cmd_pos = 0;

unsigned long last_loop_time = 0;

// --- Core Pin Functions ---
void setPin(int pin, bool state) {
  if (pin < 0 || pin >= MAX_PINS) return;
  pinMode(pin, OUTPUT);
  digitalWrite(pin, state ? HIGH : LOW);
  pinStates[pin] = state ? HIGH : LOW;
}

void reportPinState(int pin) {
  if (pin < 0 || pin >= MAX_PINS) {
    const char* msg = "ERR Invalid pin";
    Serial1.println(msg);
    Serial.println(msg);
    return;
  }
  
  char buffer[20];
  sprintf(buffer, "PIN%d STATE %s", pin, digitalRead(pin) == HIGH ? "ON" : "OFF");
  
  Serial1.println(buffer); // Report to Linux
  Serial.println(buffer);  // Report to USB Serial Monitor
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

// --- Command Parsers ---

// Handles commands coming from the USB Serial Monitor
void parseCommandFromUSB(char* command) {
  Serial.print("[USB CMD] ");
  Serial.println(command);

  // Create a mutable copy for strtok_r, as it modifies the string
  char command_copy[CMD_BUFFER_SIZE];
  strncpy(command_copy, command, CMD_BUFFER_SIZE);
  command_copy[CMD_BUFFER_SIZE - 1] = '\0';

  char* context = command_copy;
  char* commandToken = strtok_r(context, " ", &context);

  if (!commandToken) return;

  // Local commands are executed directly
  if (strcmp(commandToken, "PIN") == 0) {
    handlePinCommand(context);
  } 
  else if (strcmp(commandToken, "CONSOLE") == 0) {
    handleConsoleCommand(context);
  }
  // All other commands are forwarded to the Linux side for execution
  else {
    Serial.println("[DEBUG] Forwarding command to Linux...");
    Serial1.println(command);
  }
}

// Handles commands coming from the Linux processor (daemon)
void parseCommandFromLinux(char* command) {
  // Create a mutable copy for strtok_r
  char command_copy[CMD_BUFFER_SIZE];
  strncpy(command_copy, command, CMD_BUFFER_SIZE);
  command_copy[CMD_BUFFER_SIZE - 1] = '\0';

  char* context = command_copy;
  char* commandToken = strtok_r(context, " ", &context);

  if (!commandToken) return;

  // The daemon may ask us to execute these commands
  if (strcmp(commandToken, "PIN") == 0) {
    handlePinCommand(context);
  } 
  else if (strcmp(commandToken, "CONSOLE") == 0) {
    handleConsoleCommand(context);
  }
  // Everything else is a response or status update from the daemon.
  // We just print it to the USB Serial for debugging/feedback.
  else {
    Serial.print("[FROM LINUX] ");
    Serial.println(command);
  }
}


// --- Serial Communication & Main Loop ---

// Reusable command reader: reads a line from a stream and calls the correct parser
void readFromStream(Stream &stream, char* buffer, byte &pos, void (*parser)(char*)) {
  while (stream.available() > 0) {
    char c = stream.read();
    if (c == '\n' || c == '\r') {
      if (pos > 0) {
        buffer[pos] = '\0'; // Null-terminate the string
        parser(buffer);
        pos = 0; // Reset for the next command
      }
    } else if (pos < CMD_BUFFER_SIZE - 1) {
      buffer[pos++] = c;
    }
  }
}

void setup() {
  // Bridge.begin(); // Bridge library is not needed for direct Serial1 usage
  Serial.begin(115200);
  Serial1.begin(115200); // Ensure Serial1 is started for communication with Linux
  
  while (!Serial); // Wait for Serial Monitor to connect
  
  Serial.println("BridgeControl sketch started. Ready for commands.");
  
  for (int i = 0; i < MAX_PINS; i++) {
    pinStates[i] = LOW;
  }
  pinMode(13, OUTPUT); // Ensure LED_BUILTIN is ready
  pinStates[13] = LOW;
}

void loop() {
  // Read from Linux processor via Serial1 and parse accordingly
  readFromStream(Serial1, serial1_cmd_buffer, serial1_cmd_pos, parseCommandFromLinux);

  // Read from USB Serial Monitor via Serial and parse accordingly
  readFromStream(Serial, serial_cmd_buffer, serial_cmd_pos, parseCommandFromUSB);
}
