#pragma once

#include <stdint.h>

extern uint32_t fakeBuildCostUs;
extern bool fakeBuildResult;
extern unsigned int fakeBuildCalls;

#define buildControllerPair actualBuildControllerPair
#include "../../tx_frame_builder.h"
#undef buildControllerPair

inline bool buildControllerPair(uint8_t actionIndex, uint8_t counterStart,
                                uint32_t elapsedMs, uint16_t framesAlreadyQueued,
                                uint8_t a[22], uint8_t b[22])
{
  ++fakeBuildCalls;
  fakeClockUs += fakeBuildCostUs;
  return fakeBuildResult && actualBuildControllerPair(
      actionIndex, counterStart, elapsedMs, framesAlreadyQueued, a, b);
}
