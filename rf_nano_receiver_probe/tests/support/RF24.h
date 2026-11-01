#pragma once

#include "SPI.h"
#include <vector>

enum { RF24_1MBPS = 1, RF24_CRC_16 = 2 };

// The fake intentionally supplies no transmit/stopListening operations.
// Its assertions check CE/channel ordering and RX settling on every sample.
class RF24 {
 public:
  bool connected = true;
  bool rxMode = false;
  bool ceHigh = false;
  bool ackConfigured = false;
  bool txFlushed = false;
  uint8_t openPipes = 0x3f;
  uint8_t channel = 0;
  uint8_t addressWidth = 5;
  bool crcEnabled = true;
  bool rawPending = false;
  uint32_t rpdReads = 0;
  uint64_t ceRaisedAt = 0;
  std::vector<uint8_t> tunedChannels;

  RF24(uint8_t cePin, uint8_t csnPin) {
    assert(cePin == 10 && csnPin == 9);
  }
  bool begin() { return connected; }
  bool isChipConnected() { return connected; }
  void setAutoAck(bool enabled) {
    assert(!enabled);
    ackConfigured = true;
  }
  void flush_tx() { txFlushed = true; }
  void flush_rx() { rawPending = false; }
  void setDataRate(int rate) { assert(rate == RF24_1MBPS); }
  void setChannel(uint8_t value) {
    assert(!ceHigh);
    assert(value <= 83);
    channel = value;
    tunedChannels.push_back(value);
  }
  void startListening() {
    rxMode = true;
    ce(true);
  }
  void ce(bool high) {
    if (high) {
      assert(rxMode && ackConfigured && txFlushed);
      assert(!ceHigh);
      ceRaisedAt = fakeNowUs;
    }
    ceHigh = high;
  }
  void closeReadingPipe(uint8_t pipe) {
    assert(!ceHigh && pipe < 6);
    openPipes &= static_cast<uint8_t>(~(1 << pipe));
  }
  void setAddressWidth(uint8_t width) {
    assert(!ceHigh && (width == 2 || width == 5));
    addressWidth = width;
  }
  void disableCRC() { assert(!ceHigh); crcEnabled = false; }
  void setCRCLength(int length) {
    assert(!ceHigh && length == RF24_CRC_16);
    crcEnabled = true;
  }
  void setPayloadSize(uint8_t bytes) { assert(!ceHigh && bytes == 32); }
  void openReadingPipe(uint8_t pipe, const uint8_t* address) {
    assert(!ceHigh && pipe < 6 && addressWidth == 2 && address != nullptr);
    openPipes |= static_cast<uint8_t>(1 << pipe);
  }
  bool available(uint8_t* pipe) {
    assert(rxMode && ceHigh && !crcEnabled && addressWidth == 2 && openPipes == 0x3f);
    *pipe = 1;
    return rawPending;
  }
  void read(void* data, uint8_t bytes) {
    assert(rawPending && rxMode && bytes == 32);
    uint8_t* out = static_cast<uint8_t*>(data);
    for (uint8_t i = 0; i < bytes; ++i) { out[i] = i; }
    rawPending = false;
  }
  bool testRPD() {
    assert(rxMode && openPipes == 0);
    assert(fakeNowUs - ceRaisedAt >= 170);
    ++rpdReads;
    // Channel 3 is active only in the latched (CE LOW) reading. This tests
    // that the firmware checks the result after ending the RX interval.
    return channel == 3 && !ceHigh;
  }
};
