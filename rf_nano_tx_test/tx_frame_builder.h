#pragma once

#include <stdint.h>
#include <string.h>
#include "profile.h"

// Pure encoding only: no SPI, CE, clock, serial, or radio operations.
namespace tx_frame_detail {

static const uint8_t SCRAMBLE[11] = {
  0x80, 0xf5, 0x3b, 0x0d, 0x6d, 0x2a, 0xf9, 0xbc, 0x51, 0x8e, 0x4c
};

inline uint16_t crcBit(uint16_t crc, bool bit)
{
  const bool feedback = ((crc & 0x8000U) != 0) != bit;
  crc = static_cast<uint16_t>(crc << 1);
  return feedback ? static_cast<uint16_t>(crc ^ 0x1021U) : crc;
}

inline uint16_t crcByte(uint16_t crc, uint8_t value)
{
  for (uint8_t mask = 0x80; mask != 0; mask >>= 1) {
    crc = crcBit(crc, (value & mask) != 0);
  }
  return crc;
}

inline void encode(const uint8_t address[5], const uint8_t payload[11],
                   uint8_t pid, uint8_t output[22])
{
  memset(output, 0, 22);
  memcpy(output, address, 5);
  output[5] = address[4];
  output[6] = address[4];
  output[7] = static_cast<uint8_t>((11U << 2) | (pid & 3U));
  output[8] = 0x80; // The ninth PCF bit is NO_ACK=1.

  uint16_t crc = 0xffff;
  for (uint8_t i = 0; i < 5; ++i) {
    crc = crcByte(crc, address[i]);
  }
  // Guards are excluded from CRC. PCF and scrambled payload are MSB first.
  crc = crcByte(crc, output[7]);
  crc = crcBit(crc, true);
  for (uint8_t i = 0; i < 11; ++i) {
    const uint8_t value = static_cast<uint8_t>(payload[i] ^ SCRAMBLE[i]);
    crc = crcByte(crc, value);
    output[8 + i] |= static_cast<uint8_t>(value >> 1);
    output[9 + i] = static_cast<uint8_t>((value & 1U) << 7);
  }
  output[19] |= static_cast<uint8_t>(crc >> 9);
  output[20] = static_cast<uint8_t>(crc >> 1);
  output[21] = static_cast<uint8_t>((crc & 1U) << 7);
  // The 169-bit frame ends at bit 7 of byte 21; seven padding bits stay zero.
}

inline bool buildPair(bool advanceCounters, uint32_t elapsedMs,
                      uint16_t framesAlreadyQueued, uint8_t counterStart,
                      uint32_t counterPeriodMs, const uint8_t address[5],
                      const uint8_t profilePayload[11],
                      const uint8_t originalA[22], const uint8_t originalB[22],
                      uint8_t outputA[22], uint8_t outputB[22])
{
  if (outputA == 0 || outputB == 0 || outputA == outputB) {
    return false;
  }
  if (!advanceCounters) {
    if (originalA == 0 || originalB == 0) {
      return false;
    }
    memcpy(outputA, originalA, 22);
    memcpy(outputB, originalB, 22);
    return true;
  }
  if (address == 0 || profilePayload == 0 || counterPeriodMs == 0) {
    return false;
  }
  uint8_t payload[11];
  memcpy(payload, profilePayload, sizeof(payload));
  payload[4] = static_cast<uint8_t>(counterStart + elapsedMs / counterPeriodMs);
  uint16_t checksum = 2;
  for (uint8_t i = 0; i < 10; ++i) {
    checksum += payload[i];
  }
  payload[10] = static_cast<uint8_t>(checksum);
  const uint8_t pid = static_cast<uint8_t>(framesAlreadyQueued & 3U);
  encode(address, payload, pid, outputA);
  encode(address, payload, static_cast<uint8_t>((pid + 1U) & 3U), outputB);
  return true;
}

} // namespace tx_frame_detail

inline bool buildTxPair(uint32_t elapsedMs, uint16_t framesAlreadyQueued,
                        uint8_t a[22], uint8_t b[22])
{
  if (!PROFILE_READY || PROFILE_FRAME_SIZE != 22) {
    return false;
  }
  return tx_frame_detail::buildPair(
      PROFILE_ADVANCE_COUNTERS, elapsedMs, framesAlreadyQueued,
      PROFILE_COUNTER_START, PROFILE_COUNTER_PERIOD_MS, PROFILE_AIR_ADDRESS,
      PROFILE_PAYLOAD, PROFILE_FRAME_A, PROFILE_FRAME_B, a, b);
}
