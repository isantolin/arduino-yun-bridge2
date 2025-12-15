#include "Bridge.h"

// Serial1 is defined in test_bridge_core.cpp, so we declare it extern here if needed, 
// but Bridge constructor takes a reference.
// However, we need the object to exist.
// Since we are linking with test_bridge_core.o, and it defines Serial1, we can use it.
extern HardwareSerial Serial1;

BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;
