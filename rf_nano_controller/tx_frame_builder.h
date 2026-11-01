#pragma once

#include <stdint.h>
#include <string.h>
#include "controller_actions.h"

// Pure HS6200 encoding; no radio, serial or clock access.
namespace controller_frame_detail {
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

inline void encode(const uint8_t payload[11], uint8_t pid, uint8_t output[22])
{
  memset(output, 0, 22);
  memcpy(output, CONTROLLER_ADDRESS, 5);
  output[5] = CONTROLLER_ADDRESS[4];
  output[6] = CONTROLLER_ADDRESS[4];
  output[7] = static_cast<uint8_t>((11U << 2) | (pid & 3U));
  output[8] = 0x80;
  uint16_t crc = 0xffff;
  for (uint8_t i = 0; i < 5; ++i) { crc = crcByte(crc, CONTROLLER_ADDRESS[i]); }
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
}
} // namespace controller_frame_detail

// actionIndex comes from findControllerAction(). Both frames share one
// application counter; their PIDs advance separately through 0,1,2,3.
inline bool buildControllerPair(uint8_t actionIndex, uint8_t counterStart,
                                uint32_t elapsedMs, uint16_t framesAlreadyQueued,
                                uint8_t a[22], uint8_t b[22])
{
  if (actionIndex >= CONTROLLER_ACTION_COUNT || a == 0 || b == 0 || a == b) { return false; }
  uint8_t payload[11] = {0x15, 0x13, 0x01, 0x88, 0, 0, 0, 0, 0, 0, 0};
  payload[4] = static_cast<uint8_t>(counterStart + elapsedMs / 128);
  const ControllerAction& action = CONTROLLER_ACTIONS[actionIndex];
  payload[action.payloadSlot] = action.value;
  uint16_t checksum = 2;
  for (uint8_t i = 0; i < 10; ++i) { checksum += payload[i]; }
  payload[10] = static_cast<uint8_t>(checksum);
  const uint8_t pid = static_cast<uint8_t>(framesAlreadyQueued & 3U);
  controller_frame_detail::encode(payload, pid, a);
  controller_frame_detail::encode(payload, static_cast<uint8_t>((pid + 1U) & 3U), b);
  return true;
}
