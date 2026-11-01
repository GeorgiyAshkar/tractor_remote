// Native state-machine checks; the fake does not validate RF encoding or hardware.
#define TRACTOR_TX_NATIVE
#include "support/tx_radio.h"

#include <algorithm>
#include <iostream>
#include <limits>

uint64_t fakeClockUs = 0;
uint32_t fakeReadCostUs = 0;
uint32_t fakePrintCostUs = 0;
uint32_t fakeBuildCostUs = 0;
bool fakeBuildResult = true;
unsigned int fakeBuildCalls = 0;
FakeSerial Serial;

#include "../rf_nano_tx_test.ino"

const std::string TEST_TOKEN = "1234abcd";

void contains(const std::string& text, const std::string& expected)
{
  if (text.find(expected) == std::string::npos) {
    std::cerr << "Missing: " << expected << "\nActual: " << text << '\n';
    assert(false);
  }
}

void reset(uint64_t startUs = 0, bool found = true)
{
  fakeClockUs = startUs;
  fakeReadCostUs = 0;
  fakePrintCostUs = 0;
  fakeBuildCostUs = 0;
  fakeBuildResult = true;
  fakeBuildCalls = 0;
  Serial = FakeSerial();
  radio = TxRadio();
  radio.found = found;
  radio.mayQueue = []() { return firing && spent && !armed && framesQueued < MAX_FRAMES; };
  radioReady = false;
  armed = firing = spent = pairPending = false;
  firstPair = true;
  framesQueued = 0;
  armedAtMs = heartbeatAtMs = burstStartedUs = lastPairUs = pairStartedUs = 0;
  memset(token, 0, sizeof(token));
  memset(commandBuffer, 0, sizeof(commandBuffer));
  commandLength = 0;
  commandOverflow = false;
  setup();
}

std::string command(const std::string& text)
{
  Serial.output.clear();
  Serial.feed(text + "\n");
  while (Serial.available()) { loop(); }
  return Serial.output;
}

void arm()
{
  contains(command(std::string("ARM ") + PROFILE_ID + " " + TEST_TOKEN), "ARMED,");
  assert(armed && !firing && !spent && radio.pairs.empty() && !radio.powered);
}

void fire()
{
  arm();
  const auto output = command("FIRE " + TEST_TOKEN);
  contains(output, "START,");
  contains(output, "duration_ms=" + std::to_string(MAX_BURST_US / 1000));
  assert(firing && spent && !armed && radio.pairs.empty() && radio.powered);
}

void advance(uint32_t us, bool keep = false)
{
  fakeClockUs += us;
  if (keep && firing) {
    command("KEEP " + TEST_TOKEN);
  } else {
    loop();
  }
}

void assertOff()
{
  assert(!firing && !armed && !pairPending && !radio.powered && !radio.pending);
}

void completeWithHostHeartbeat()
{
  uint64_t lastKeep = fakeClockUs;
  uint32_t previousFrames = framesQueued;
  // Model the host's 25 ms cadence rather than refreshing on every tick.
  for (uint32_t i = 0; i < MAX_BURST_US / 200 + 1000 && firing; ++i) {
    advance(200);
    if (firing && fakeClockUs - lastKeep >= 25000) {
      command("KEEP " + TEST_TOKEN);
      lastKeep = fakeClockUs;
    }
    // Long profiles must cross 255 without wrapping the firmware counter.
    assert(framesQueued >= previousFrames);
    assert(framesQueued == radio.pairs.size() * 2);
    previousFrames = framesQueued;
  }
}

void testBootAndUnarmedInput()
{
  reset();
  contains(Serial.output, "BOOT,protocol=tractortx-v1,armed=no,radio=off");
  contains(Serial.output, "duration_ms=" + std::to_string(MAX_BURST_US / 1000) +
                         ",max_frames=" + std::to_string(static_cast<unsigned int>(MAX_FRAMES)));
  assertOff();
  for (const auto& text : {"STATUS", "HELLO", "PING", "FIRE 1234abcd", "BOGUS", "STOP"}) {
    command(text);
    assertOff();
    assert(radio.pairs.empty() && radio.powerCalls == 0);
  }
  Serial.feed(std::string("ARM ") + PROFILE_ID + " " + TEST_TOKEN);
  loop();
  assert(!armed && !firing && radio.pairs.empty());
  Serial.feed("\n");
  loop();
  assert(armed && radio.pairs.empty());
}

void testProfileAndTokenValidation()
{
  const std::string prefix = std::string("ARM ") + PROFILE_ID + " ";
  for (const auto& text : {std::string("ARM 00000000 1234abcd"), prefix + "1234abc", prefix + "1234abcde",
                          prefix + "1234abcG", prefix + "1234ABCD", prefix + "1234abcd extra"}) {
    reset();
    contains(command(text), "ERR,profile_or_token");
    assertOff();
  }
  reset(); arm();
  contains(command("FIRE deadbeef"), "ERR,not_armed_or_expired");
  assertOff();
  assert(!spent && radio.pairs.empty());
  arm();
  contains(command("KEEP deadbeef"), "ERR,token");
  assertOff();
}

void testArmExpiryAndRollover()
{
  for (const uint64_t start : {uint64_t(0), uint64_t(0xfffffffdUL) * 1000}) {
    reset(start); arm();
    advance(4999000);
    assert(armed);
    advance(1000);
    assert(!armed);
    command("FIRE " + TEST_TOKEN);
    assertOff();
    assert(radio.powerCalls == 0 && radio.pairs.empty());
  }
}

void testFiniteBurstAndOneShot(uint64_t start)
{
  assert(MAX_FRAMES <= std::numeric_limits<decltype(framesQueued)>::max());
  reset(start); fire();
  const uint64_t began = fakeClockUs;
  Serial.output.clear();
  completeWithHostHeartbeat();
  assertOff();
  assert(spent && framesQueued == MAX_FRAMES && radio.pairs.size() == MAX_FRAMES / 2);
  assert(fakeClockUs - began <= MAX_BURST_US);
  contains(Serial.output, "reason=complete,frames=" + std::to_string(static_cast<unsigned int>(MAX_FRAMES)));
  for (const auto& pair : radio.pairs) {
    assert(pair.queuedUs - began >= POWERUP_US);
    assert(pair.queuedUs - began <= MAX_BURST_US - PAIR_RESERVE_US);
    assert(pair.channel == PROFILE_CHANNEL);
    assert(pair.a.size() == PROFILE_FRAME_SIZE && pair.b.size() == PROFILE_FRAME_SIZE);
    // Dynamic counter/PID/CRC generation is checked in builder tests. The
    // engine must retain the learned address and guard envelope on every pair.
    assert(std::equal(pair.a.begin(), pair.a.begin() + 7, PROFILE_FRAME_A));
    assert(std::equal(pair.b.begin(), pair.b.begin() + 7, PROFILE_FRAME_B));
  }
  const auto n = radio.pairs.size();
  contains(command(std::string("ARM ") + PROFILE_ID + " " + TEST_TOKEN), "ERR,spent_reset_required");
  command("FIRE " + TEST_TOKEN);
  advance(500000);
  assertOff();
  assert(radio.pairs.size() == n);
}

void testLateHeartbeatAfterCompletion()
{
  reset(); fire();
  completeWithHostHeartbeat();
  assertOff();
  assert(spent && framesQueued == MAX_FRAMES);
  const auto queued = radio.pairs.size();
  const auto powerCalls = radio.powerCalls;
  const auto sleepCalls = radio.sleepCalls;
  const auto heartbeat = heartbeatAtMs;

  assert(command("KEEP " + TEST_TOKEN).empty());
  assertOff();
  assert(spent && radio.pairs.size() == queued);
  assert(radio.powerCalls == powerCalls && radio.sleepCalls == sleepCalls);
  assert(heartbeatAtMs == heartbeat);

  assert(command("STOP") == "OK,stopped=1\n");
  assertOff();
  assert(command("FIRE " + TEST_TOKEN) == "ERR,not_armed_or_expired\n");
  assert(command("KEEP deadbeef") == "ERR,token\n");
  assertOff();
  advance(500000);
  assertOff();
  assert(spent && radio.pairs.size() == queued && radio.powerCalls == powerCalls);
}

void testDeadlineMargin()
{
  reset(); fire();
  fakeClockUs = burstStartedUs + MAX_BURST_US - PAIR_RESERVE_US + 1;
  heartbeatAtMs = millis();
  loop();
  assert(firing && radio.pairs.empty());
  fakeClockUs = burstStartedUs + MAX_BURST_US;
  heartbeatAtMs = millis();
  loop();
  assertOff();
  assert(radio.pairs.empty());
  contains(Serial.output, "reason=complete,frames=0,elapsed_ms=" + std::to_string(MAX_BURST_US / 1000));

  reset(); fire();
  fakeClockUs = burstStartedUs + MAX_BURST_US - PAIR_RESERVE_US;
  heartbeatAtMs = millis();
  loop();
  assert(firing && radio.pairs.size() == 1 && framesQueued == 2);
  advance(1500, true);
  assert(!pairPending);
  fakeClockUs = burstStartedUs + MAX_BURST_US;
  loop();
  assertOff();
}

void testHeartbeatAndMicrosRollover()
{
  for (const uint64_t start : {uint64_t(0), uint64_t(0xfffff000UL), uint64_t(0xfffffffdUL) * 1000}) {
    reset(start); fire();
    advance(5000);
    assert(radio.pairs.size() == 1);
    // Heartbeat uses millisecond granularity, including across millis wrap.
    const uint64_t expiresAt = start - start % 1000 + 100000;
    fakeClockUs = expiresAt - 1;
    loop();
    assert(firing);
    fakeClockUs = expiresAt;
    loop();
    assertOff();
    contains(Serial.output, "reason=heartbeat");
    const auto n = radio.pairs.size();
    command("KEEP " + TEST_TOKEN);
    advance(100000);
    assert(radio.pairs.size() == n && !firing);
  }
}

void testStopAndUnexpectedCommand()
{
  for (const auto& text : {"STOP", "STATUS", "PING", "HELLO", "FIRE 1234abcd", "UNKNOWN", "KEEP deadbeef"}) {
    reset(); fire(); advance(5000);
    assert(radio.pairs.size() == 1 && pairPending);
    const auto before = radio.pairs.size();
    command(text);
    assertOff();
    assert(spent && radio.sleepCalls >= 2);
    advance(500000);
    assert(radio.pairs.size() == before);
  }
  reset(); arm(); command("STOP"); assertOff();
}

void testRadioFailures()
{
  reset(0, false);
  contains(command(std::string("ARM ") + PROFILE_ID + " " + TEST_TOKEN), "ERR,radio_or_profile_not_ready");
  command("FIRE " + TEST_TOKEN);
  assertOff();
  assert(radio.powerCalls == 0 && radio.pairs.empty());

  reset(); arm(); radio.powerUpResult = false;
  contains(command("FIRE " + TEST_TOKEN), "reason=radio_error");
  assertOff(); assert(spent && radio.pairs.empty());

  reset(); fire(); radio.queueResult = false;
  advance(5000);
  assertOff(); assert(spent && radio.pairs.empty() && framesQueued == 0);
  contains(Serial.output, "reason=radio_error");

  reset(); fire(); radio.stuck = true;
  advance(5000);
  const uint64_t waitingAt = fakeClockUs;
  fakeClockUs = waitingAt + PAIR_TIMEOUT_US - 1;
  loop(); assert(firing);
  fakeClockUs = waitingAt + PAIR_TIMEOUT_US;
  loop(); assertOff();
  contains(Serial.output, "reason=radio_timeout");

  // Hardware wait begins after frame-building/queueing work has returned.
  // A slower queue call must not consume the radio's completion allowance.
  reset(); fire(); radio.queueCostUs = 1500;
  advance(5000);
  const uint64_t returnedAt = fakeClockUs;
  assert(pairStartedUs == micros());
  fakeClockUs = returnedAt + 500;
  loop(); assert(firing && pairPending);
  fakeClockUs = returnedAt + 1000;
  loop(); assert(firing && !pairPending);
  assert(Serial.output.find("radio_timeout") == std::string::npos);
  command("STOP"); assertOff();
}

void testBuilderFailureAndTiming()
{
  reset(); fire(); fakeBuildResult = false;
  advance(POWERUP_US);
  assertOff();
  assert(spent && framesQueued == 0 && radio.pairs.empty() && fakeBuildCalls == 1);
  contains(Serial.output, "reason=radio_error");

  // A build leaving less than the radio's final 3 ms allowance cannot queue.
  reset(); fire();
  fakeClockUs = burstStartedUs + MAX_BURST_US - PAIR_RESERVE_US;
  heartbeatAtMs = millis();
  fakeBuildCostUs = PAIR_RESERVE_US - 3000 + 1;
  loop();
  assert(firing && radio.pairs.empty() && framesQueued == 0);
  fakeClockUs = burstStartedUs + MAX_BURST_US;
  loop(); assertOff();
  assert(radio.pairs.empty());

  // Even a pathological overrun must not enqueue a packet after the deadline.
  reset(); fire();
  fakeClockUs = burstStartedUs + MAX_BURST_US - PAIR_RESERVE_US;
  heartbeatAtMs = millis();
  fakeBuildCostUs = PAIR_RESERVE_US + 1;
  loop(); assertOff();
  assert(spent && radio.pairs.empty() && framesQueued == 0);

  // Builder execution cannot conceal an expired heartbeat before queueing.
  reset(); fire();
  fakeClockUs = burstStartedUs + 99000;
  fakeBuildCostUs = 1500;
  loop(); assertOff();
  assert(radio.pairs.empty() && framesQueued == 0);
  contains(Serial.output, "reason=heartbeat");

  // Build and queue costs do not consume the subsequent radio-wait timeout.
  reset(); fire();
  fakeBuildCostUs = 1500;
  radio.queueCostUs = 1500;
  advance(POWERUP_US);
  const uint64_t returnedAt = fakeClockUs;
  assert(pairStartedUs == micros());
  fakeClockUs = returnedAt + 500;
  loop(); assert(firing && pairPending);
  fakeClockUs = returnedAt + 1000;
  loop(); assert(firing && !pairPending);
  command("STOP"); assertOff();
}

void testOverflowAndInputBudget()
{
  reset(); arm();
  command(std::string(48, 'X') + "FIRE " + TEST_TOKEN);
  assertOff(); assert(radio.pairs.empty() && !spent);
  contains(Serial.output, "ERR,invalid_command_line");
  assert(command("PING") == "PONG\n");

  reset(); fire(); advance(5000);
  fakeReadCostUs = 10;
  Serial.feed(std::string(20000, 'X'));
  const uint64_t before = fakeClockUs;
  loop();
  assert(fakeClockUs - before == 480);
  assert(Serial.available() == 20000 - 48);
  assertOff();
  assert(radio.pairs.size() == 1);
}

void testEmbeddedNulLineRejected()
{
  reset(); arm();
  const std::string malformed = "FIRE " + TEST_TOKEN + std::string(1, '\0') + "junk";
  command(malformed);
  assertOff();
  assert(radio.pairs.empty() && radio.powerCalls == 0 && !spent);
}

int main()
{
  testBootAndUnarmedInput();
  testProfileAndTokenValidation();
  testArmExpiryAndRollover();
  testFiniteBurstAndOneShot(0);
  testFiniteBurstAndOneShot(0xfffff000UL);
  testLateHeartbeatAfterCompletion();
  testDeadlineMargin();
  testHeartbeatAndMicrosRollover();
  testStopAndUnexpectedCommand();
  testRadioFailures();
  testBuilderFailureAndTiming();
  testOverflowAndInputBudget();
  testEmbeddedNulLineRejected();
  std::cout << "TX firmware boundary tests passed (13 scenarios).\n";
}
