#include <SPI.h>
#include <RF24.h>
#include <string.h>

const uint8_t RADIO_CE_PIN = 10;
const uint8_t RADIO_CSN_PIN = 9;
const uint8_t FIRST_RF_CHANNEL = 0;
const uint8_t LAST_RF_CHANNEL = 83;
// Nordic specifies 130 us RX settling plus 40 us for a valid RPD reading.
const uint16_t SAMPLE_US = 250;
const uint8_t SAMPLES_PER_CHANNEL = 32;
const uint16_t DWELL_REPORT_MS = 1000;
const uint32_t RAW_CHANNEL_US = 20000;
// Experimental preamble-like addresses from the RF24 project's scanner:
// https://github.com/nRF24/RF24/blob/master/examples/scanner/scanner.ino
const uint8_t RAW_ADDRESSES[6][2] = {
  {0x55, 0x55}, {0xAA, 0xAA}, {0xA0, 0xAA},
  {0xAB, 0xAA}, {0xAC, 0xAA}, {0xAD, 0xAA}
};

RF24 radio(RADIO_CE_PIN, RADIO_CSN_PIN);

bool radioReady = false;
bool scanning = false;
bool dwellMode = false;
bool rawMode = false;
bool rawFixedChannel = false;
bool rawVisitActive = false;
uint8_t rawChannel = FIRST_RF_CHANNEL;
uint32_t rawVisitStartedUs = 0;
uint32_t rawRound = 0;
uint32_t rawRoundRecords = 0;
uint8_t dwellChannel = 3;
uint8_t scanChannel = FIRST_RF_CHANNEL;
uint8_t channelSamples = 0;
uint8_t scanHits[LAST_RF_CHANNEL + 1] = {};
uint32_t scanRound = 0;
uint32_t dwellSamples = 0;
uint32_t dwellHits = 0;
uint32_t windowStartedMs = 0;
char commandBuffer[32];
uint8_t commandLength = 0;
bool commandOverflow = false;

void printStatus()
{
  Serial.print(F("STATUS,radio="));
  Serial.print(radioReady ? F("ready") : F("error"));
  Serial.print(F(",scan="));
  Serial.print(scanning ? F("on") : F("off"));
  Serial.print(F(",mode="));
  Serial.print(rawMode ? F("raw") : (dwellMode ? F("dwell") : F("scan")));
  Serial.print(F(",channel="));
  Serial.print(dwellChannel);
  Serial.print(F(",raw_fixed="));
  Serial.print(rawFixedChannel ? F("on") : F("off"));
  Serial.print(F(",sample_us="));
  Serial.print(SAMPLE_US);
  Serial.print(F(",samples_per_channel="));
  Serial.println(SAMPLES_PER_CHANNEL);
}

void printHelp()
{
  Serial.println(F("COMMANDS,PING|STATUS|SCAN ON|SCAN OFF|MODE SCAN|MODE DWELL|MODE RAW|CHANNEL 0..83|HELP"));
}

void reportDwell()
{
  if (dwellSamples == 0) {
    return;
  }
  const uint32_t now = millis();
  Serial.print(F("DWELL,channel="));
  Serial.print(dwellChannel);
  Serial.print(F(",mhz="));
  Serial.print(2400 + dwellChannel);
  Serial.print(F(",samples="));
  Serial.print(dwellSamples);
  Serial.print(F(",hits="));
  Serial.print(dwellHits);
  Serial.print(F(",elapsed_ms="));
  Serial.println(now - windowStartedMs);
  dwellSamples = 0;
  dwellHits = 0;
  windowStartedMs = millis();
}

void resetMeasurements()
{
  // Keep PRIM_RX=1 throughout operation. CE is never raised in TX mode.
  if (radioReady) {
    radio.ce(LOW);
  }
  memset(scanHits, 0, sizeof(scanHits));
  scanChannel = FIRST_RF_CHANNEL;
  channelSamples = 0;
  dwellSamples = 0;
  dwellHits = 0;
  windowStartedMs = millis();
  rawVisitActive = false;
  rawChannel = rawMode && rawFixedChannel ? dwellChannel : FIRST_RF_CHANNEL;
  rawRoundRecords = 0;
}

void configureReception()
{
  if (!radioReady) {
    return;
  }
  radio.ce(LOW);
  radio.flush_rx();
  for (uint8_t pipe = 0; pipe < 6; ++pipe) {
    radio.closeReadingPipe(pipe);
  }
  if (rawMode) {
    // This undocumented 2-byte address tactic can return noise/fragments.
    // No CRC, address, packet framing, or protocol validity is claimed.
    radio.disableCRC();
    radio.setAddressWidth(2);
    radio.setPayloadSize(32);
    for (uint8_t pipe = 0; pipe < 6; ++pipe) {
      radio.openReadingPipe(pipe, RAW_ADDRESSES[pipe]);
    }
  } else {
    radio.setAddressWidth(5);
    radio.setCRCLength(RF24_CRC_16);
  }
}

void finishPartialWindow()
{
  if (radioReady) {
    radio.ce(LOW);
  }
  if (scanning && dwellMode) {
    reportDwell();
  }
}

bool parseChannel(const char* text, uint8_t& channel)
{
  if (*text == '\0') {
    return false;
  }
  uint16_t value = 0;
  for (; *text != '\0'; ++text) {
    if (*text < '0' || *text > '9') {
      return false;
    }
    value = value * 10 + (*text - '0');
    if (value > LAST_RF_CHANNEL) {
      return false;
    }
  }
  channel = static_cast<uint8_t>(value);
  return true;
}

void processCommand()
{
  commandBuffer[commandLength] = '\0';

  if (strcmp(commandBuffer, "PING") == 0) {
    Serial.println(F("PONG"));
  } else if (strcmp(commandBuffer, "STATUS") == 0) {
    printStatus();
  } else if (strcmp(commandBuffer, "SCAN ON") == 0) {
    if (!radioReady) {
      Serial.println(F("ERR,radio_not_ready"));
    } else {
      if (!scanning) {
        resetMeasurements();
      }
      scanning = true;
      Serial.println(F("OK,scan=on"));
    }
  } else if (strcmp(commandBuffer, "SCAN OFF") == 0) {
    finishPartialWindow();
    scanning = false;
    resetMeasurements();
    Serial.println(F("OK,scan=off"));
  } else if (strcmp(commandBuffer, "MODE SCAN") == 0 ||
             strcmp(commandBuffer, "MODE DWELL") == 0 ||
             strcmp(commandBuffer, "MODE RAW") == 0) {
    finishPartialWindow();
    dwellMode = strcmp(commandBuffer, "MODE DWELL") == 0;
    rawMode = strcmp(commandBuffer, "MODE RAW") == 0;
    rawFixedChannel = false;
    resetMeasurements();
    configureReception();
    Serial.print(F("OK,mode="));
    Serial.println(rawMode ? F("raw") : (dwellMode ? F("dwell") : F("scan")));
  } else if (strncmp(commandBuffer, "CHANNEL ", 8) == 0) {
    uint8_t channel;
    if (!parseChannel(commandBuffer + 8, channel)) {
      Serial.println(F("ERR,channel_must_be_0..83"));
    } else {
      finishPartialWindow();
      dwellChannel = channel;
      if (rawMode) {
        rawFixedChannel = true;
      }
      resetMeasurements();
      Serial.print(F("OK,channel="));
      Serial.println(dwellChannel);
    }
  } else if (strcmp(commandBuffer, "HELP") == 0) {
    printHelp();
  } else if (commandLength > 0) {
    Serial.println(F("ERR,unknown_command"));
  }

  commandLength = 0;
}

void pollSerial()
{
  while (Serial.available() > 0) {
    const char incoming = static_cast<char>(Serial.read());
    if (incoming == '\r') {
      continue;
    }
    if (incoming == '\n') {
      if (commandOverflow) {
        commandOverflow = false;
        commandLength = 0;
        Serial.println(F("ERR,command_too_long"));
      } else {
        processCommand();
      }
      continue;
    }
    // Discard the entire oversized line, including any command-like suffix.
    if (commandOverflow) {
      continue;
    }
    if (commandLength < sizeof(commandBuffer) - 1) {
      commandBuffer[commandLength++] = incoming;
    } else {
      commandOverflow = true;
    }
  }
}

bool sampleRPD()
{
  radio.ce(HIGH);
  delayMicroseconds(SAMPLE_US);
  const bool before = radio.testRPD();
  radio.ce(LOW);
  const bool after = radio.testRPD();
  return before || after;
}

void reportRound()
{
  ++scanRound;
  uint8_t activeChannels = 0;
  for (uint8_t channel = FIRST_RF_CHANNEL; channel <= LAST_RF_CHANNEL; ++channel) {
    if (scanHits[channel] == 0) {
      continue;
    }
    ++activeChannels;
    Serial.print(F("HIT,round="));
    Serial.print(scanRound);
    Serial.print(F(",channel="));
    Serial.print(channel);
    Serial.print(F(",mhz="));
    Serial.print(2400 + channel);
    Serial.print(F(",samples="));
    Serial.print(SAMPLES_PER_CHANNEL);
    Serial.print(F(",hits="));
    Serial.println(scanHits[channel]);
  }
  Serial.print(F("ROUND,number="));
  Serial.print(scanRound);
  Serial.print(F(",active_channels="));
  Serial.print(activeChannels);
  Serial.print(F(",samples_per_channel="));
  Serial.println(SAMPLES_PER_CHANNEL);
  memset(scanHits, 0, sizeof(scanHits));
}

void scanOneSample()
{
  if (channelSamples == 0) {
    radio.setChannel(scanChannel);
  }
  if (sampleRPD()) {
    ++scanHits[scanChannel];
  }
  if (++channelSamples == SAMPLES_PER_CHANNEL) {
    channelSamples = 0;
    if (scanChannel == LAST_RF_CHANNEL) {
      reportRound();
      scanChannel = FIRST_RF_CHANNEL;
    } else {
      ++scanChannel;
    }
  }
}

void dwellOneSample()
{
  // Channel writes are kept outside the high-CE receive interval.
  if (dwellSamples == 0) {
    radio.setChannel(dwellChannel);
  }
  if (sampleRPD()) {
    ++dwellHits;
  }
  ++dwellSamples;
  if (millis() - windowStartedMs >= DWELL_REPORT_MS) {
    reportDwell();
  }
}

void rawOneStep()
{
  if (!rawVisitActive) {
    radio.ce(LOW);
    radio.flush_rx();
    radio.setChannel(rawChannel);
    rawVisitStartedUs = micros();
    radio.ce(HIGH);
    rawVisitActive = true;
  }

  uint8_t pipe = 0;
  if (radio.available(&pipe)) {
    uint8_t fragment[32];
    radio.read(fragment, sizeof(fragment));
    radio.ce(LOW);
    ++rawRoundRecords;
    Serial.print(F("RAW,channel="));
    Serial.print(rawChannel);
    Serial.print(F(",mhz="));
    Serial.print(2400 + rawChannel);
    Serial.print(F(",pipe="));
    Serial.print(pipe);
    Serial.print(F(",bytes=32,hex="));
    const char digits[] = "0123456789abcdef";
    for (uint8_t i = 0; i < sizeof(fragment); ++i) {
      Serial.print(digits[fragment[i] >> 4]);
      Serial.print(digits[fragment[i] & 0x0F]);
    }
    Serial.println();
    radio.ce(HIGH);
  }

  if (!rawFixedChannel && micros() - rawVisitStartedUs >= RAW_CHANNEL_US) {
    radio.ce(LOW);
    radio.flush_rx();
    rawVisitActive = false;
    if (rawChannel == LAST_RF_CHANNEL) {
      rawChannel = FIRST_RF_CHANNEL;
      Serial.print(F("RAW_ROUND,number="));
      Serial.print(++rawRound);
      Serial.print(F(",records="));
      Serial.println(rawRoundRecords);
      rawRoundRecords = 0;
    } else {
      ++rawChannel;
    }
  }
}

void setup()
{
  Serial.begin(115200);
  Serial.println(F("BOOT,protocol=rfprobe-v2,mode=receive-only"));
  radioReady = radio.begin() && radio.isChipConnected();
  if (!radioReady) {
    Serial.println(F("ERR,radio_not_detected,check_RF-Nano_revision_and_pins"));
    printStatus();
    return;
  }

  radio.setAutoAck(false);
  radio.flush_tx();
  radio.flush_rx();
  radio.setDataRate(RF24_1MBPS);
  radio.setChannel(FIRST_RF_CHANNEL);
  radio.startListening();
  radio.ce(LOW);
  configureReception();

  Serial.println(F("RADIO,connected=1,ce=10,csn=9,ack=off,tx=never"));
  printHelp();
  printStatus();
  resetMeasurements();
}

void loop()
{
  pollSerial();
  if (radioReady && scanning) {
    if (rawMode) {
      rawOneStep();
    } else if (dwellMode) {
      dwellOneSample();
    } else {
      scanOneSample();
    }
  }
}
