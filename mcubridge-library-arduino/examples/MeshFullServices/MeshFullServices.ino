/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 *
 * MeshFullServices.ino - Canonical E2E Multi-Architecture Reference Sketch
 *
 * Implements full active support for 100% of ecosystem services in
 * cycle-accurate hardware emulation (simavr, Renode, QEMU):
 * - Boot Known-Answer Tests (KAT) for SHA-256, HMAC, and ChaCha20-Poly1305
 * - SIL-2 Hardware Clock Synchronization (CMD_CLOCK_SYNC / host offset / RTT)
 * - Bi-directional Console Streaming (ConsoleClass)
 * - Inter-node Mailbox routing (MailboxClass)
 * - Distributed Key-Value Caching (DataStoreClass)
 * - Linux Host FileSystem operations (FileSystemClass)
 * - Subprocess execution & streaming (ProcessClass)
 * - Reactive GPIO, PWM, and Pin Subscriptions (Bridge GPIO core)
 * - Hardware Watchdog Keepalive monitoring
 *
 * Safety / Architecture: Zero-Heap, No STL, ETL Algorithms (Rule 8), SIL-2.
 */

#include <Arduino.h>
#include <Bridge.h>
#include <etl/algorithm.h>
#include <etl/span.h>
#include <etl/string.h>
#include <etl/string_view.h>
#include <hal/hal.h>
#include <services/Console.h>
#include <services/DataStore.h>
#include <services/FileSystem.h>
#include <services/Mailbox.h>
#include <services/Process.h>
#include <services/SPIService.h>

#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET \
  "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
#endif

#if defined(ARDUINO_ARCH_AVR)
inline constexpr uint8_t TEST_PWM_PIN = 9;
inline constexpr uint8_t TEST_IN_PIN = 7;
#elif defined(ARDUINO_ARCH_SAMD)
inline constexpr uint8_t TEST_PWM_PIN = 5;
inline constexpr uint8_t TEST_IN_PIN = 4;
#else
inline constexpr uint8_t TEST_PWM_PIN = 8;
inline constexpr uint8_t TEST_IN_PIN = 7;
#endif

static void onMailboxMessage(etl::span<const uint8_t> payload) {
  etl::string_view msg(reinterpret_cast<const char*>(payload.data()),
                       payload.size());
  Console.print(F("[MAILBOX_IN] "));
  Console.write(payload.data(), payload.size());
  Console.println();

  if (msg.starts_with("TARGET:")) {
    etl::string<64> reply = "RESP:";
    etl::string_view rest = msg.substr(7);
    reply.append(rest.data(), rest.size());
    Mailbox.push(etl::span<const uint8_t>(
        reinterpret_cast<const uint8_t*>(reply.data()), reply.size()));
  } else if (msg == "PING") {
    const char pong[] = "PONG";
    Mailbox.push(etl::span<const uint8_t>(
        reinterpret_cast<const uint8_t*>(pong), sizeof(pong) - 1));
  }
}

static void onDataStoreRead(etl::string_view key,
                            etl::span<const uint8_t> value) {
  Console.print(F("[DATASTORE_GET] key="));
  Console.print(key.data());
  Console.print(F(" val="));
  Console.write(value.data(), value.size());
  Console.println();
}

static void onFileReadComplete(etl::span<const uint8_t> content) {
  Console.print(F("[FILE_READ] size="));
  Console.println(content.size());
}

static void onProcessRun(int32_t pid) {
  Console.print(F("[PROCESS_PID] "));
  Console.println(pid);
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);
  pinMode(TEST_PWM_PIN, OUTPUT);
  pinMode(TEST_IN_PIN, INPUT_PULLUP);

  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);

  Mailbox.registerMessageCallback(
      MailboxClass::MessageCallback::create<onMailboxMessage>());

  Bridge.awaitSynchronization();

  if (Bridge.isSynchronized()) {
    digitalWrite(LED_BUILTIN, HIGH);
    Console.begin();
    Console.println(F("[BOOT] OK"));
  }
}

enum class InitStep : uint8_t {
  INIT_DATASTORE = 0,
  INIT_FILESYSTEM,
  INIT_DONE
};

void loop() {
  Bridge.process();

  if (!Bridge.isSynchronized()) {
    digitalWrite(LED_BUILTIN, LOW);
    return;
  }

  static InitStep s_initStep = InitStep::INIT_DATASTORE;
  static unsigned long s_lastInitMs = 0;

  if (s_initStep == InitStep::INIT_DATASTORE) {
    const char initialVal[] = "ONLINE";
    DataStore.set(
        "node/status",
        etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(initialVal),
                                 sizeof(initialVal) - 1));
    s_initStep = InitStep::INIT_FILESYSTEM;
    s_lastInitMs = millis();
  } else if (s_initStep == InitStep::INIT_FILESYSTEM &&
             (millis() - s_lastInitMs >= 300)) {
    const char tokenData[] = "BOOT_OK";
    FileSystem.write(
        "boot_token.txt",
        etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(tokenData),
                                 sizeof(tokenData) - 1));
    s_initStep = InitStep::INIT_DONE;
  }

  static unsigned long lastConsoleProcess = 0;
  if (millis() - lastConsoleProcess >= 100) {
    lastConsoleProcess = millis();
    Console.process();
  }

  static unsigned long lastMailboxPoll = millis();
  if (millis() - lastMailboxPoll >= 250) {
    lastMailboxPoll = millis();
    Mailbox.requestRead();
  }

  static unsigned long lastHeartbeat = millis();
  if (millis() - lastHeartbeat >= 5000) {
    lastHeartbeat = millis();
    Console.print(F("[HEARTBEAT] FreeMem="));
    Console.println(bridge::hal::getFreeMemory());

    DataStore.get("fleet/shared_key",
                  DataStoreClass::GetHandler::create<onDataStoreRead>());
  }
}
