#pragma once

#include <stdint.h>

extern uint32_t fakeBuildCostUs;
extern bool fakeBuildResult;
extern unsigned int fakeBuildCalls;

// Use the real pure encoder; inject only elapsed time and a failure result.
#define buildTxPair actualBuildTxPair
#include "../../tx_frame_builder.h"
#undef buildTxPair

inline bool buildTxPair(uint32_t elapsedMs, uint16_t framesAlreadyQueued,
                        uint8_t a[22], uint8_t b[22])
{
  ++fakeBuildCalls;
  fakeClockUs += fakeBuildCostUs;
  return fakeBuildResult && actualBuildTxPair(elapsedMs, framesAlreadyQueued, a, b);
}
