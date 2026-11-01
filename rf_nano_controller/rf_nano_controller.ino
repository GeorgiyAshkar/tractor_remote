#include <stdint.h>
#include <string.h>
#include "controller_actions.h"

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
uint8_t actionIndex = 0;
uint8_t counterStart = 0;
char lastFiredToken[9] = {};
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

void emitReady(bool boot = false) {
  Serial.print(boot ? F("BOOT") : F("READY"));
  Serial.print(F(",protocol=tractorctl-v1,firmware=web-control-v1,radio="));
  Serial.print(radioReady ? F("ready") : F("error"));
  Serial.print(F(",armed=")); Serial.print(armed ? F("yes") : F("no"));
  Serial.print(F(",active=")); Serial.print(firing ? F("yes") : F("no"));
  Serial.print(F(",max_duration_ms=")); Serial.print(MAX_BURST_US / 1000);
  Serial.print(F(",heartbeat_ms=")); Serial.print(HEARTBEAT_TIMEOUT_MS);
  Serial.print(F(",max_frames=")); Serial.println(MAX_FRAMES);
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
    Serial.print(F(",radio=off,action="));
    Serial.println(CONTROLLER_ACTIONS[actionIndex].code);
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

int8_t hexDigit(char c) {
  if (c >= '0' && c <= '9') { return c - '0'; }
  if (c >= 'a' && c <= 'f') { return c - 'a' + 10; }
  return -1;
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
    } else if (!armed && !firing && hexToken(commandBuffer + 5) &&
               strcmp(lastFiredToken, commandBuffer + 5) == 0) {
      // The UART may already contain the previous operation's final KEEP.
    } else {
      stopTransmission("serial_error");
      Serial.println(F("ERR,token"));
    }
  } else if (firing) {
    stopTransmission("serial_error");
    Serial.println(F("ERR,busy"));
  } else if (strcmp(commandBuffer, "HELLO") == 0 || strcmp(commandBuffer, "STATUS") == 0) {
    emitReady();
  } else if (strcmp(commandBuffer, "PING") == 0) {
    Serial.println(F("PONG"));
  } else if (strncmp(commandBuffer, "ARM ", 4) == 0) {
    armed = false;
    if (!radioReady) {
      Serial.println(F("ERR,radio_not_ready"));
    } else if (strlen(commandBuffer) != 18 || commandBuffer[6] != ' ' ||
               commandBuffer[9] != ' ' || !hexToken(commandBuffer + 10)) {
      Serial.println(F("ERR,action_counter_or_token"));
    } else {
      const int8_t selected = findControllerAction(commandBuffer + 4);
      const int8_t hi = hexDigit(commandBuffer[7]);
      const int8_t lo = hexDigit(commandBuffer[8]);
      if (selected < 0 || hi < 0 || lo < 0) {
        Serial.println(F("ERR,action_counter_or_token"));
      } else if (strcmp(lastFiredToken, commandBuffer + 10) == 0) {
        Serial.println(F("ERR,token_reused"));
      } else {
        actionIndex = static_cast<uint8_t>(selected);
        counterStart = static_cast<uint8_t>((hi << 4) | lo);
        memcpy(token, commandBuffer + 10, sizeof(token));
        armedAtMs = millis();
        heartbeatAtMs = armedAtMs;
        armed = true;
        Serial.print(F("ARMED,action=")); Serial.print(CONTROLLER_ACTIONS[actionIndex].code);
        Serial.print(F(",counter=")); Serial.print(commandBuffer[7]); Serial.print(commandBuffer[8]);
        Serial.print(F(",token=")); Serial.println(token);
      }
    }
  } else if (strncmp(commandBuffer, "FIRE ", 5) == 0) {
    if (!armed || !matchesToken(commandBuffer + 5) ||
        static_cast<uint32_t>(millis() - armedAtMs) >= ARM_TIMEOUT_MS) {
      armed = false;
      Serial.println(F("ERR,not_armed_or_expired"));
    } else {
      armed = false;
      memcpy(lastFiredToken, token, sizeof(lastFiredToken));
      framesQueued = 0;
      pairPending = false;
      firstPair = true;
      burstStartedUs = micros();
      heartbeatAtMs = millis();
      firing = true;
      if (!radio.powerUp(CONTROLLER_ACTIONS[actionIndex].channel)) {
        stopTransmission("radio_error");
      } else {
        Serial.print(F("START,token=")); Serial.print(token);
        Serial.print(F(",duration_ms=")); Serial.print(MAX_BURST_US / 1000);
        Serial.print(F(",action=")); Serial.println(CONTROLLER_ACTIONS[actionIndex].code);
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
  if (!buildControllerPair(actionIndex, counterStart, elapsed / 1000, framesQueued, frameA, frameB)) {
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
  if (!radio.queuePair(frameA, frameB, CONTROLLER_FRAME_SIZE)) {
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
  // Uploading or opening USB initializes powered down; ARM/FIRE are explicit.
  emitReady(true);
  emitReady();
}

void loop() {
  tickTransmission();
  pollSerial();
  tickTransmission();
}
