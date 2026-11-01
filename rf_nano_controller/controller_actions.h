#pragma once

#include <stdint.h>
#include <string.h>

struct ControllerAction {
  char code[3];
  uint8_t channel;
  uint8_t payloadSlot;
  uint8_t value;
};

static const ControllerAction CONTROLLER_ACTIONS[] = {
  {"TL", 15, 6, 0x04}, {"TR", 15, 6, 0x0d},
  {"BU", 31, 7, 0xd0}, {"BD", 31, 7, 0x70},
  {"LF", 31, 5, 0x07}, {"LB", 31, 5, 0x0f},
  {"RF", 31, 5, 0x70}, {"RB", 31, 5, 0xf0},
  {"FF", 31, 5, 0x77}
};
static const uint8_t CONTROLLER_ACTION_COUNT = sizeof(CONTROLLER_ACTIONS) / sizeof(CONTROLLER_ACTIONS[0]);
static const uint8_t CONTROLLER_ADDRESS[5] = {0x8d, 0x5f, 0xb5, 0xcb, 0x75};
static const uint8_t CONTROLLER_FRAME_SIZE = 22;

inline int8_t findControllerAction(const char* code)
{
  for (uint8_t i = 0; i < CONTROLLER_ACTION_COUNT; ++i) {
    if (code[0] == CONTROLLER_ACTIONS[i].code[0] && code[1] == CONTROLLER_ACTIONS[i].code[1]) {
      return static_cast<int8_t>(i);
    }
  }
  return -1;
}
