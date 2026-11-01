#pragma once

#include <assert.h>
#include <stdint.h>
#include <deque>
#include <functional>
#include <sstream>
#include <string>
#include <vector>

#define F(text) text

extern uint64_t fakeClockUs;
extern uint32_t fakeReadCostUs;
extern uint32_t fakePrintCostUs;

inline uint32_t micros() { return static_cast<uint32_t>(fakeClockUs); }
inline uint32_t millis() { return static_cast<uint32_t>(fakeClockUs / 1000); }

class FakeSerial {
 public:
  std::deque<char> input;
  std::string output;
  void begin(unsigned long baud) { assert(baud == 115200); }
  int available() { return static_cast<int>(input.size()); }
  int read() {
    const unsigned char c = static_cast<unsigned char>(input.front());
    input.pop_front();
    fakeClockUs += fakeReadCostUs;
    return c;
  }
  void feed(const std::string& text) { input.insert(input.end(), text.begin(), text.end()); }
  template <typename T> void print(const T& value) {
    std::ostringstream text;
    text << value;
    output += text.str();
    fakeClockUs += fakePrintCostUs;
  }
  void print(uint8_t value) { print(static_cast<unsigned int>(value)); }
  template <typename T> void println(const T& value) { print(value); output += '\n'; }
};

extern FakeSerial Serial;

class TxRadio {
 public:
  struct Pair {
    uint64_t queuedUs;
    uint8_t channel;
    std::vector<uint8_t> a;
    std::vector<uint8_t> b;
  };
  bool found = true;
  bool powerUpResult = true;
  bool queueResult = true;
  bool stuck = false;
  bool powered = false;
  bool pending = false;
  uint8_t channel = 0;
  uint32_t queueCostUs = 400;
  uint32_t completionUs = 1000;
  uint64_t pendingSinceUs = 0;
  unsigned int sleepCalls = 0;
  unsigned int powerCalls = 0;
  std::vector<Pair> pairs;
  std::vector<uint64_t> sleeps;
  std::function<bool()> mayQueue;

  bool begin() { sleep(); return found; }
  void sleep() {
    powered = false;
    pending = false;
    ++sleepCalls;
    sleeps.push_back(fakeClockUs);
  }
  bool powerUp(uint8_t value) {
    ++powerCalls;
    channel = value;
    powered = powerUpResult;
    return powerUpResult;
  }
  bool queuePair(const uint8_t* a, const uint8_t* b, uint8_t bytes) {
    assert(powered && !pending && bytes == 22);
    assert(mayQueue && mayQueue());
    if (!queueResult) { return false; }
    pairs.push_back(Pair{fakeClockUs, channel,
                         std::vector<uint8_t>(a, a + bytes),
                         std::vector<uint8_t>(b, b + bytes)});
    fakeClockUs += queueCostUs;
    pendingSinceUs = fakeClockUs;
    pending = true;
    return true;
  }
  bool pairDone() {
    assert(powered);
    if (stuck || fakeClockUs - pendingSinceUs < completionUs) { return false; }
    pending = false;
    return true;
  }
};
