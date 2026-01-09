/*
  Arduino Yun USB-to-Serial Terminal (250000 Baud - Experimental)

  Serial terminal for Arduino Yun using 250000 baud throughout
  (matching U-Boot native speed). No baudrate switching needed.

  Features:
  - Fixed 250000 baud for both U-Boot and Linux
  - Manual baudrate override with ~ commands
  - Bridge shutdown command support

  Upload this via USB (not WiFi) then open Serial Monitor at 250000.

  Commands (press ~ followed by):
    '0' -> Set UART to 57600 baud
    '1' -> Set UART to 115200 baud  
    '2' -> Set UART to 250000 baud
    '3' -> Set UART to 500000 baud
    '~' -> Send bridge shutdown command

  Boot sequence:
    Both U-Boot and Linux run at 250000 baud (no auto-switching)

  Created 2013 by Massimo Banzi
  Modified by Cristian Maglie
  Enhanced 2026 for Arduino Yun Bridge v2 (experimental/250000-baud)

  This example code is in the public domain.
*/

// Configuration - Experimental 250000 baud throughout
#define DEFAULT_BAUD  250000   // Both U-Boot and Linux
#define USB_BAUD      250000   // USB serial monitor baudrate

// Current state
static long currentBaud = DEFAULT_BAUD;
static bool commandMode = false;

void setup() {
  SERIAL_PORT_USBVIRTUAL.begin(USB_BAUD);
  SERIAL_PORT_HARDWARE.begin(currentBaud);
  
  // Wait for USB serial
  while (!SERIAL_PORT_USBVIRTUAL) {
    ; // Wait for connection
  }
  
  printBanner();
}

void printBanner() {
  SERIAL_PORT_USBVIRTUAL.println();
  SERIAL_PORT_USBVIRTUAL.println(F("=== Arduino Yun Serial Terminal (250k Experimental) ==="));
  SERIAL_PORT_USBVIRTUAL.print(F("Current baud: "));
  SERIAL_PORT_USBVIRTUAL.println(currentBaud);
  SERIAL_PORT_USBVIRTUAL.println(F("Commands: ~0=57600 ~1=115200 ~2=250000 ~3=500000 ~~=shutdown"));
  SERIAL_PORT_USBVIRTUAL.println();
}

void setBaudRate(long baud, const char* reason) {
  currentBaud = baud;
  SERIAL_PORT_HARDWARE.end();
  delay(10);
  SERIAL_PORT_HARDWARE.begin(currentBaud);
  
  SERIAL_PORT_USBVIRTUAL.print(F("\r\n[BAUD] "));
  SERIAL_PORT_USBVIRTUAL.print(currentBaud);
  if (reason) {
    SERIAL_PORT_USBVIRTUAL.print(F(" ("));
    SERIAL_PORT_USBVIRTUAL.print(reason);
    SERIAL_PORT_USBVIRTUAL.print(F(")"));
  }
  SERIAL_PORT_USBVIRTUAL.println();
}

void processCommand(int c) {
  switch (c) {
    case '0':
      setBaudRate(57600, "manual");
      break;
      
    case '1':
      setBaudRate(115200, "manual");
      break;
      
    case '2':
      setBaudRate(250000, "manual");
      break;
      
    case '3':
      setBaudRate(500000, "manual");
      break;
      
    case 'r':
    case 'R':
      // Reset to default baud
      setBaudRate(DEFAULT_BAUD, "reset");
      break;
      
    case '~':
      // Bridge shutdown command
      SERIAL_PORT_HARDWARE.write((uint8_t *)"\xff\0\0\x05XXXXX\x7f\xf9", 11);
      SERIAL_PORT_USBVIRTUAL.println(F("\r\n[CMD] Sent bridge shutdown command"));
      break;
      
    case '?':
    case 'h':
    case 'H':
      printBanner();
      break;
      
    default:
      // Not a command, send both ~ and the char
      SERIAL_PORT_HARDWARE.write('~');
      SERIAL_PORT_HARDWARE.write(c);
      break;
  }
}

void loop() {
  // USB -> Linux UART
  int c = SERIAL_PORT_USBVIRTUAL.read();
  if (c != -1) {
    if (!commandMode) {
      if (c == '~') {
        commandMode = true;
      } else {
        SERIAL_PORT_HARDWARE.write(c);
      }
    } else {
      processCommand(c);
      commandMode = false;
    }
  }
  
  // Linux UART -> USB (simple passthrough at 250000 baud)
  c = SERIAL_PORT_HARDWARE.read();
  if (c != -1) {
    SERIAL_PORT_USBVIRTUAL.write(c);
  }
}
