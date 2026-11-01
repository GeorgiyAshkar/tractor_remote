// Native control-flow tests. Real RF behavior is verified on the board.
#include "support/SPI.h"
#include "support/RF24.h"

#include <iostream>

uint64_t fakeNowUs = 0;
FakeSerial Serial;

#include "../rf_nano_receiver_probe.ino"

void reset(bool connected = true)
{
  fakeNowUs = 0;
  Serial = FakeSerial();
  radio = RF24(10, 9);
  radio.connected = connected;
  radioReady = false;
  scanning = false;
  dwellMode = false;
  rawMode = false;
  rawFixedChannel = false;
  rawRound = 0;
  dwellChannel = 3;
  scanRound = 0;
  commandLength = 0;
  commandOverflow = false;
  resetMeasurements();
  setup();
}

std::string command(const std::string& text)
{
  Serial.output.clear();
  Serial.feed(text + "\n");
  pollSerial();
  return Serial.output;
}

void contains(const std::string& text, const std::string& expected)
{
  if (text.find(expected) == std::string::npos) {
    std::cerr << "Missing: " << expected << "\nActual: " << text << '\n';
    assert(false);
  }
}

void testBootAndCommands()
{
  reset();
  contains(Serial.output, "BOOT,protocol=rfprobe-v2");
  contains(Serial.output, "STATUS,radio=ready,scan=off,mode=scan,channel=3");
  assert(radioReady && !scanning && !radio.ceHigh && radio.rxMode);
  const uint32_t reads = radio.rpdReads;
  loop();
  assert(radio.rpdReads == reads);
  assert(command("PING") == "PONG\n");
  contains(command("HELP"), "MODE DWELL|MODE RAW|CHANNEL 0..83");
  assert(command("SCAN ON") == "OK,scan=on\n");
  loop();
  assert(radio.rpdReads == reads + 2 && !radio.ceHigh);
  assert(command("SCAN OFF") == "OK,scan=off\n");
  assert(!scanning && scanChannel == 0 && channelSamples == 0);
}

void testChannelValidationAndOversizedLines()
{
  reset();
  assert(command("CHANNEL 0") == "OK,channel=0\n");
  assert(command("CHANNEL 83") == "OK,channel=83\n");
  for (const char* value : {"84", "99999", "-1", "+3", "3x", "", " 3", "3 "}) {
    assert(command(std::string("CHANNEL ") + value) == "ERR,channel_must_be_0..83\n");
    assert(dwellChannel == 83);
  }
  assert(command(std::string(32, 'X') + "SCAN ON") == "ERR,command_too_long\n");
  assert(!scanning);
  assert(command("PING") == "PONG\n");
  Serial.output.clear();
  Serial.feed("MODE DW");
  pollSerial();
  assert(Serial.output.empty() && !dwellMode);
  Serial.feed("ELL\r\nPING\n");
  pollSerial();
  assert(Serial.output == "OK,mode=dwell\nPONG\n");
  assert(dwellMode);
}

void testCompletedAndInterruptedScan()
{
  reset();
  command("SCAN ON");
  Serial.output.clear();
  for (unsigned int i = 0; i < 84 * 32; ++i) {
    loop();
  }
  assert(scanRound == 1 && scanChannel == 0 && channelSamples == 0);
  assert(radio.rpdReads == 84 * 32 * 2);
  assert(Serial.output == "HIT,round=1,channel=3,mhz=2403,samples=32,hits=32\n"
                          "ROUND,number=1,active_channels=1,samples_per_channel=32\n");
  // No stale hits may survive stopping an incomplete sweep.
  for (unsigned int i = 0; i < 4 * 32; ++i) {
    loop();
  }
  assert(scanHits[3] == 32);
  command("SCAN OFF");
  assert(scanHits[3] == 0);
  command("SCAN ON");
  assert(scanChannel == 0 && channelSamples == 0);
  assert(!radio.ceHigh && radio.rxMode);
}

void testDwellAndPartialWindow()
{
  reset();
  command("MODE DWELL");
  command("CHANNEL 3");
  command("SCAN ON");
  Serial.output.clear();
  for (unsigned int i = 0; i < 4000; ++i) {
    loop();
  }
  assert(Serial.output == "DWELL,channel=3,mhz=2403,samples=4000,hits=4000,elapsed_ms=1000\n");
  assert(dwellSamples == 0 && dwellHits == 0);
  for (unsigned int i = 0; i < 20; ++i) {
    loop();
  }
  assert(command("SCAN OFF") == "DWELL,channel=3,mhz=2403,samples=20,hits=20,elapsed_ms=5\n"
                                 "OK,scan=off\n");
  assert(!scanning && !radio.ceHigh && dwellSamples == 0);
  command("SCAN ON");
  for (unsigned int i = 0; i < 8; ++i) {
    loop();
  }
  assert(command("CHANNEL 4") == "DWELL,channel=3,mhz=2403,samples=8,hits=8,elapsed_ms=2\n"
                                  "OK,channel=4\n");
  assert(scanning && dwellChannel == 4);
  for (unsigned int i = 0; i < 4; ++i) {
    loop();
  }
  assert(command("MODE SCAN") == "DWELL,channel=4,mhz=2404,samples=4,hits=0,elapsed_ms=1\n"
                                  "OK,mode=scan\n");
  assert(scanning && !dwellMode && !radio.ceHigh);
}

void testRadioFailure()
{
  reset(false);
  contains(Serial.output, "ERR,radio_not_detected");
  contains(Serial.output, "STATUS,radio=error,scan=off");
  assert(command("SCAN ON") == "ERR,radio_not_ready\n");
  assert(command("SCAN OFF") == "OK,scan=off\n");
  loop();
  assert(!radioReady && !scanning && radio.rpdReads == 0 && !radio.ceHigh);
}

void testRawScanAndReturnToEnergyMode()
{
  reset();
  assert(command("MODE RAW") == "OK,mode=raw\n");
  assert(rawMode && !dwellMode && !radio.crcEnabled && radio.addressWidth == 2);
  assert(radio.openPipes == 0x3f && !radio.ceHigh);
  command("SCAN ON");
  Serial.output.clear();
  loop();
  assert(rawChannel == 0 && rawVisitActive && radio.ceHigh);
  radio.rawPending = true;
  loop();
  assert(Serial.output == "RAW,channel=0,mhz=2400,pipe=1,bytes=32,hex="
                          "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f\n");
  assert(rawRoundRecords == 1 && radio.ceHigh);
  for (unsigned int channel = 0; channel < 84; ++channel) {
    fakeNowUs += RAW_CHANNEL_US;
    loop();
    assert(!radio.ceHigh);
    if (channel < 83) { loop(); }
  }
  contains(Serial.output, "RAW_ROUND,number=1,records=1\n");
  assert(rawChannel == 0 && !rawVisitActive);
  assert(command("SCAN OFF") == "OK,scan=off\n");
  assert(!radio.ceHigh && !scanning);
  assert(command("MODE DWELL") == "OK,mode=dwell\n");
  assert(!rawMode && dwellMode && radio.openPipes == 0 && radio.crcEnabled);
  command("SCAN ON");
  loop();
  assert(radio.rpdReads == 2 && !radio.ceHigh);
}

void testRawFixedChannel()
{
  reset();
  command("MODE RAW");
  command("CHANNEL 15");
  assert(rawFixedChannel && rawChannel == 15);
  command("SCAN ON");
  loop();
  fakeNowUs += RAW_CHANNEL_US * 100;
  loop();
  assert(rawChannel == 15 && rawVisitActive && radio.ceHigh);
  radio.rawPending = true;
  loop();
  contains(Serial.output, "RAW,channel=15,mhz=2415");
  assert(command("CHANNEL 31") == "OK,channel=31\n");
  loop();
  assert(rawChannel == 31 && radio.channel == 31 && radio.ceHigh);
  command("SCAN OFF");
  assert(!radio.ceHigh);
  command("MODE RAW");
  assert(!rawFixedChannel && rawChannel == 0);
}

int main()
{
  testBootAndCommands();
  testChannelValidationAndOversizedLines();
  testCompletedAndInterruptedScan();
  testDwellAndPartialWindow();
  testRadioFailure();
  testRawScanAndReturnToEnergyMode();
  testRawFixedChannel();
  std::cout << "Firmware control-flow tests passed (7 cases).\n";
}
