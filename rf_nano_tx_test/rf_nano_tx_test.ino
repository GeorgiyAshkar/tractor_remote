#include <stdint.h>
#include <string.h>
#include "profile.h"

#ifdef TRACTOR_TX_NATIVE
#include "tests/support/tx_radio.h"
#include "tests/support/tx_frame_builder.h"
#else
#include "tx_radio.h"
#include "tx_frame_builder.h"
#endif

const uint32_t MAX_BURST_US = 1000000;
const uint32_t PAIR_PERIOD_US = 10000;
const uint32_t POWERUP_US = 5000;
const uint32_t PAIR_TIMEOUT_US = 2000;
const uint32_t PAIR_RESERVE_US = 5000;
const uint32_t ARM_TIMEOUT_MS = 5000;
const uint32_t HEARTBEAT_TIMEOUT_MS = 100;
const uint16_t MAX_FRAMES = 200;

TxRadio radio;
bool radioReady = false;
bool armed = false;
bool firing = false;
bool spent = false;
bool pairPending = false;
bool firstPair = true;
uint16_t framesQueued = 0;
uint32_t armedAtMs = 0;
uint32_t heartbeatAtMs = 0;
uint32_t burstStartedUs = 0;
uint32_t lastPairUs = 0;
uint32_t pairStartedUs = 0;
char token[9] = {};
char commandBuffer[48];
uint8_t commandLength = 0;
bool commandOverflow = false;

void emitReady() {
  Serial.print(F("READY,protocol=tractortx-v1,radio="));
  Serial.print(radioReady ? F("ready") : F("error"));
  Serial.print(F(",profile=")); Serial.print(PROFILE_ID);
  Serial.print(F(",channel=")); Serial.print(PROFILE_CHANNEL);
  Serial.print(F(",duration_ms=")); Serial.print(MAX_BURST_US / 1000);
  Serial.print(F(",max_frames=")); Serial.print(MAX_FRAMES);
  Serial.print(F(",armed="));
  Serial.print(armed ? F("yes") : F("no"));
  Serial.print(F(",spent=")); Serial.println(spent ? F("yes") : F("no"));
}

void stopTransmission(const char* reason) {
  const bool wasFiring = firing;
  const uint32_t elapsed = wasFiring ? static_cast<uint32_t>(micros() - burstStartedUs) / 1000 : 0;
  armed = false;
  firing = false;
  pairPending = false;
  if (radioReady) { radio.sleep(); }
  if (wasFiring) {
    Serial.print(F("DONE,token=")); Serial.print(token);
    Serial.print(F(",reason=")); Serial.print(reason);
    Serial.print(F(",frames=")); Serial.print(framesQueued);
    Serial.print(F(",elapsed_ms=")); Serial.print(elapsed);
    Serial.println(F(",radio=off"));
  }
}

bool hexToken(const char* value) {
  for (uint8_t i = 0; i < 8; ++i) {
    const char c = value[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) { return false; }
  }
  return value[8] == '\0';
}

bool matchesToken(const char* value) {
  return hexToken(value) && strcmp(token, value) == 0;
}

void processCommand() {
  commandBuffer[commandLength] = '\0';
  if (commandLength == 0) { return; }

  if (strcmp(commandBuffer, "STOP") == 0) {
    stopTransmission("stop");
    Serial.println(F("OK,stopped=1"));
  } else if (strncmp(commandBuffer, "KEEP ", 5) == 0) {
    if ((armed || firing) && matchesToken(commandBuffer + 5)) {
      heartbeatAtMs = millis();
    } else if (spent && !armed && !firing && matchesToken(commandBuffer + 5)) {
      // A final heartbeat can already be in the UART when DONE is delivered.
      // Ignore it after this consumed one-shot; it cannot rearm or transmit.
    } else {
      stopTransmission("serial_error");
      Serial.println(F("ERR,token"));
    }
  } else if (firing) {
    // Avoid serial printing or configuration during an active burst.
    stopTransmission("serial_error");
    Serial.println(F("ERR,busy"));
  } else if (strcmp(commandBuffer, "HELLO") == 0 || strcmp(commandBuffer, "STATUS") == 0) {
    emitReady();
  } else if (strcmp(commandBuffer, "PING") == 0) {
    Serial.println(F("PONG"));
  } else if (strncmp(commandBuffer, "ARM ", 4) == 0) {
    armed = false;
    if (!radioReady || !PROFILE_READY) {
      Serial.println(F("ERR,radio_or_profile_not_ready"));
    } else if (spent) {
      Serial.println(F("ERR,spent_reset_required"));
    } else if (strlen(commandBuffer) != 21 || commandBuffer[12] != ' ' ||
               strncmp(commandBuffer + 4, PROFILE_ID, 8) != 0 ||
               !hexToken(commandBuffer + 13)) {
      Serial.println(F("ERR,profile_or_token"));
    } else {
      memcpy(token, commandBuffer + 13, sizeof(token));
      armedAtMs = millis();
      heartbeatAtMs = armedAtMs;
      armed = true;
      Serial.print(F("ARMED,profile=")); Serial.print(PROFILE_ID);
      Serial.print(F(",token=")); Serial.println(token);
    }
  } else if (strncmp(commandBuffer, "FIRE ", 5) == 0) {
    if (!armed || spent || !matchesToken(commandBuffer + 5) ||
        static_cast<uint32_t>(millis() - armedAtMs) >= ARM_TIMEOUT_MS) {
      armed = false;
      Serial.println(F("ERR,not_armed_or_expired"));
    } else {
      // Consume the one-shot before doing any hardware work.
      armed = false;
      spent = true;
      framesQueued = 0;
      pairPending = false;
      firstPair = true;
      burstStartedUs = micros();
      heartbeatAtMs = millis();
      firing = true;
      if (!radio.powerUp(PROFILE_CHANNEL)) {
        stopTransmission("radio_error");
      } else {
        Serial.print(F("START,token=")); Serial.print(token);
        Serial.print(F(",duration_ms=")); Serial.println(MAX_BURST_US / 1000);
      }
    }
  } else {
    armed = false;
    Serial.println(F("ERR,unknown_command"));
  }
}

void pollSerial() {
  // Bound input work per iteration so heartbeat and deadline checks run often.
  uint8_t budget = 48;
  while (Serial.available() > 0 && budget-- > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r') { continue; }
    const uint8_t byte = static_cast<uint8_t>(c);
    if ((byte < 32 && c != '\n') || byte > 126) {
      // Never allow an embedded NUL to truncate a longer command into FIRE.
      commandOverflow = true;
      commandLength = 0;
      stopTransmission("serial_error");
      continue;
    }
    if (c == '\n') {
      if (commandOverflow) {
        commandOverflow = false;
        commandLength = 0;
        Serial.println(F("ERR,invalid_command_line"));
      } else {
        processCommand();
        commandLength = 0;
      }
    } else if (!commandOverflow) {
      if (commandLength < sizeof(commandBuffer) - 1) {
        commandBuffer[commandLength++] = c;
      } else {
        commandOverflow = true;
        commandLength = 0;
        stopTransmission("serial_error");
      }
    }
  }
}

void tickTransmission() {
  if (armed && static_cast<uint32_t>(millis() - armedAtMs) >= ARM_TIMEOUT_MS) {
    armed = false;
  }
  if (!firing) { return; }
  const uint32_t now = micros();
  const uint32_t elapsed = now - burstStartedUs;
  if (elapsed >= MAX_BURST_US) {
    stopTransmission("complete");
    return;
  }
  if (static_cast<uint32_t>(millis() - heartbeatAtMs) >= HEARTBEAT_TIMEOUT_MS) {
    stopTransmission("heartbeat");
    return;
  }
  if (pairPending) {
    if (radio.pairDone()) {
      pairPending = false;
    } else if (static_cast<uint32_t>(micros() - pairStartedUs) >= PAIR_TIMEOUT_US) {
      stopTransmission("radio_timeout");
    }
    return;
  }
  if (framesQueued >= MAX_FRAMES) {
    stopTransmission("complete");
    return;
  }
  // Never start a pair close enough to the deadline to overrun it on air.
  if (elapsed < POWERUP_US || MAX_BURST_US - elapsed < PAIR_RESERVE_US) { return; }
  if (!firstPair && static_cast<uint32_t>(now - lastPairUs) < PAIR_PERIOD_US) { return; }
  uint8_t frameA[22];
  uint8_t frameB[22];
  if (!buildTxPair(elapsed / 1000, framesQueued, frameA, frameB)) {
    stopTransmission("radio_error");
    return;
  }
  // Encoding must not let a delayed loop enqueue a packet beyond the deadline.
  const uint32_t afterBuild = micros() - burstStartedUs;
  if (afterBuild >= MAX_BURST_US) {
    stopTransmission("complete");
    return;
  }
  if (MAX_BURST_US - afterBuild < 3000) { return; }
  if (static_cast<uint32_t>(millis() - heartbeatAtMs) >= HEARTBEAT_TIMEOUT_MS) {
    stopTransmission("heartbeat");
    return;
  }
  lastPairUs = now;
  if (!radio.queuePair(frameA, frameB, PROFILE_FRAME_SIZE)) {
    stopTransmission("radio_error");
    return;
  }
  pairStartedUs = micros();
  framesQueued += 2;
  firstPair = false;
  pairPending = true;
}

void setup() {
  Serial.begin(115200);
  radioReady = radio.begin();
  // Initialization ends powered down. Uploading/opening the port never fires.
  Serial.println(F("BOOT,protocol=tractortx-v1,armed=no,radio=off"));
  emitReady();
}

void loop() {
  tickTransmission();
  pollSerial();
  tickTransmission();
}
