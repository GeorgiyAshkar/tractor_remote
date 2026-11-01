#pragma once

#include <assert.h>
#include <stdint.h>
#include <deque>
#include <sstream>
#include <string>

#define F(text) text
#define LOW false
#define HIGH true

extern uint64_t fakeNowUs;

inline uint32_t micros() { return static_cast<uint32_t>(fakeNowUs); }
inline uint32_t millis() { return static_cast<uint32_t>(fakeNowUs / 1000); }
inline void delayMicroseconds(uint16_t us) { fakeNowUs += us; }

class FakeSerial {
 public:
  std::deque<char> input;
  std::string output;

  void begin(unsigned long baud) { assert(baud == 115200); }
  int available() { return static_cast<int>(input.size()); }
  char read() {
    const char value = input.front();
    input.pop_front();
    return value;
  }
  void feed(const std::string& text) {
    input.insert(input.end(), text.begin(), text.end());
  }
  template <typename T> void print(const T& value) {
    std::ostringstream text;
    text << value;
    output += text.str();
  }
  void print(uint8_t value) { print(static_cast<unsigned int>(value)); }
  void println() { output += '\n'; }
  template <typename T> void println(const T& value) {
    print(value);
    output += '\n';
  }
};

extern FakeSerial Serial;
