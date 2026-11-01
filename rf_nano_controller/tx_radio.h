#pragma once

#include <SPI.h>
#include <RF24.h>

// Bounded legacy ShockBurst sender. No packet is queued during initialization.
// EN_AA=0 and ARC=0 remove the hardware PCF (Nordic specification section 7.9).
// Manual HS6200 address/guards/PCF/CRC are supplied in the captured 22-byte frame.
class TxRadio {
 public:
  TxRadio() : radio_(10, 9), pairStartedUs_(0) {}

  bool begin() {
    const bool found = radio_.begin() && radio_.isChipConnected();
    radio_.ce(LOW);
    if (!found) { return false; }
    sleep();
    writeReg(0x01, 0);       // EN_AA: no automatic acknowledgements.
    writeReg(0x02, 0);       // EN_RXADDR: all receive pipes closed.
    writeReg(0x03, 3);       // SETUP_AW: five-byte hardware address.
    writeReg(0x04, 0);       // SETUP_RETR: no automatic retransmissions.
    writeReg(0x05, 15);      // Initial channel; actual profile sets it at FIRE.
    writeReg(0x06, 1);       // 1 Mbps, PA_MIN (-18 dBm), LNA bit retained.
    writeReg(0x1C, 0);       // DYNPD disabled.
    writeReg(0x1D, 0);       // FEATURE disabled.
    const uint8_t preamble[5] = {0x55, 0x55, 0x55, 0x55, 0x55};
    writeBytes(0x30, preamble, sizeof(preamble)); // W_REGISTER | TX_ADDR.
    writeReg(0x07, 0x70);    // Clear interrupt status.
    command(0xE2);           // Flush RX.
    return readReg(0x00) == 0x70 && readReg(0x01) == 0 &&
           readReg(0x02) == 0 && readReg(0x03) == 3 &&
           readReg(0x04) == 0 && readReg(0x06) == 1 &&
           readReg(0x1C) == 0 && readReg(0x1D) == 0;
  }

  bool powerUp(uint8_t channel) {
    radio_.ce(LOW);
    command(0xE1);
    writeReg(0x05, channel);
    writeReg(0x00, 0x72);    // Interrupts masked, PWR_UP, TX, no hardware CRC.
    return readReg(0x05) == channel && readReg(0x00) == 0x72;
  }

  bool queuePair(const uint8_t* a, const uint8_t* b, uint8_t bytes) {
    if (bytes != 22) { return false; }
    radio_.ce(LOW);
    command(0xE1);
    writeReg(0x07, 0x70);
    // The address's MSB is 1, so 0x55 hardware address extends the preamble.
    // Payloads are exact captured envelopes, not guessed movement values.
    writeBytes(0xA0, a, bytes);
    writeBytes(0xA0, b, bytes);
    pairStartedUs_ = micros();
    radio_.ce(HIGH);
    return true;
  }

  bool pairDone() {
    // Two 224-us legacy packets plus RX/TX settling fit well below 1 ms.
    // Waiting avoids mistaking FIFO dequeue for completion of the final packet.
    if (static_cast<uint32_t>(micros() - pairStartedUs_) < 1000) { return false; }
    if ((readReg(0x17) & 0x10) == 0 || (readReg(0x07) & 0x20) == 0) {
      return false;
    }
    radio_.ce(LOW);
    return true;
  }

  void sleep() {
    radio_.ce(LOW);
    command(0xE1);           // No pending/reused packet may survive stopping.
    writeReg(0x00, 0x70);    // PWR_DOWN, no CRC, TX disabled by power state.
  }

 private:
  RF24 radio_;
  uint32_t pairStartedUs_;

  void beginSpi() {
    SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
    digitalWrite(9, LOW);
  }
  void endSpi() {
    digitalWrite(9, HIGH);
    SPI.endTransaction();
  }
  void command(uint8_t opcode) {
    beginSpi();
    SPI.transfer(opcode);
    endSpi();
  }
  uint8_t readReg(uint8_t reg) {
    beginSpi();
    SPI.transfer(reg & 0x1F);
    const uint8_t value = SPI.transfer(0xFF);
    endSpi();
    return value;
  }
  void writeReg(uint8_t reg, uint8_t value) {
    beginSpi();
    SPI.transfer(0x20 | (reg & 0x1F));
    SPI.transfer(value);
    endSpi();
  }
  void writeBytes(uint8_t opcode, const uint8_t* bytes, uint8_t count) {
    beginSpi();
    SPI.transfer(opcode);
    for (uint8_t i = 0; i < count; ++i) { SPI.transfer(bytes[i]); }
    endSpi();
  }
};
