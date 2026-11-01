// Pure encoder tests: captured bytes and an independent bit-stream parser.
#include "../tx_frame_builder.h"

#include <assert.h>
#include <iostream>
#include <string>
#include <vector>

const uint8_t AIR_ADDRESS[5] = {0x8d, 0x5f, 0xb5, 0xcb, 0x75};
const uint8_t RIGHT_PAYLOAD[11] = {0x15, 0x13, 0x01, 0x88, 0x66, 0x00, 0xae, 0x00, 0x00, 0x00, 0xc7};
const uint8_t RX_XOR[11] = {0x80, 0xf5, 0x3b, 0x0d, 0x6d, 0x2a, 0xf9, 0xbc, 0x51, 0x8e, 0x4c};

std::vector<uint8_t> hexBytes(const std::string& hex)
{
  assert(hex.size() % 2 == 0);
  std::vector<uint8_t> bytes;
  for (size_t i = 0; i < hex.size(); i += 2) {
    bytes.push_back(static_cast<uint8_t>(std::stoul(hex.substr(i, 2), 0, 16)));
  }
  return bytes;
}

struct Decoded {
  uint8_t pid;
  uint8_t noAck;
  uint8_t payload[11];
  uint16_t crc;
};

Decoded parse(const uint8_t frame[22])
{
  // Deliberately use a bit-string representation instead of the encoder's
  // packed-byte expressions, so shifted PCF/payload/CRC mistakes are visible.
  std::string bits;
  for (unsigned int i = 0; i < 22; ++i) {
    for (int bit = 7; bit >= 0; --bit) {
      bits += ((frame[i] >> bit) & 1) ? '1' : '0';
    }
  }
  auto number = [&bits](size_t begin, size_t count) {
    return static_cast<unsigned int>(std::stoul(bits.substr(begin, count), 0, 2));
  };
  for (unsigned int i = 0; i < 5; ++i) { assert(number(i * 8, 8) == AIR_ADDRESS[i]); }
  assert(number(40, 8) == AIR_ADDRESS[4] && number(48, 8) == AIR_ADDRESS[4]);
  assert(number(56, 6) == 11);
  Decoded out = {};
  out.pid = static_cast<uint8_t>(number(62, 2));
  out.noAck = static_cast<uint8_t>(number(64, 1));
  for (unsigned int i = 0; i < 11; ++i) {
    out.payload[i] = static_cast<uint8_t>(number(65 + i * 8, 8) ^ RX_XOR[i]);
  }
  out.crc = static_cast<uint16_t>(number(153, 16));
  const std::string checked = bits.substr(0, 40) + bits.substr(56, 97);
  unsigned int remainder = 0xffff;
  for (char bit : checked) {
    const unsigned int incoming = bit == '1' ? 1 : 0;
    const unsigned int overflow = (remainder >> 15) ^ incoming;
    remainder = (remainder << 1) & 0xffff;
    if (overflow) { remainder ^= 0x1021; }
  }
  assert(out.crc == remainder);
  assert(bits.substr(169) == "0000000");
  return out;
}

void testCapturedFixtures()
{
  struct Fixture {
    const char* payload;
    uint8_t pid;
    const char* captured;
    uint16_t crc;
    uint8_t bitOffset;
    Fixture(const char* data, uint8_t identity, const char* raw, uint16_t expectedCrc,
            uint8_t offset = 0)
        : payload(data), pid(identity), captured(raw), crc(expectedCrc), bitOffset(offset) {}
  };
  // Full-address RAW captures from the original tower-right sample and the
  // physically confirmed boom-DOWN sample. The latter retains its historical
  // boom_raise_pulse_raw.log filename; source lines are 4393 and 4398.
  const Fixture fixtures[] = {
    {"151301886600ae000000c7", 3, "8d5fb5cb7575752fcaf31d4285952bde28c745f9430aa5a555af557a5bf5852c", 0xf286},
    {"151301887a00ae000000db", 0, "8d5fb5cb7575752ccaf31d428b952bde28c74ba78ad615a81d51232ca97adcb5", 0x4f15},
    {"151301888800ae000000e9", 2, "8d5fb5cb7575752ecaf31d42f2952bde28c7528dd5d5042516b9149a7375558a", 0x1bab},
    {"151301886d000072000092", 3, "8d5fb5cb7575752fcaf31d4280157ce728c76f7c940be42880045688b05512a9", 0xf928},
    {"151301886d000072000092", 2, "8d5fb5cb7575752ecaf31d4280157ce728c76f7fe10414081066089117492087", 0xffc2},
    // 2026-11-01_left_track_forward_raw.log, lines 4605 and 4606.
    {"15130188b207000000006c", 1, "8d5fb5cb7575752dcaf31d42ef96fcde28c7102b7b132a2850195b05bd344853", 0x56f6},
    {"15130188b207000000006c", 2, "8d5fb5cb7575752ecaf31d42ef96fcde28c7102ee409815297a1ea49f4bb0e55", 0x5dc8},
    // left_track_backward_raw.log is a historical filename: the operator
    // actually held RIGHT FORWARD. Lines 17851/17852; the latter starts at bit 1.
    {"15130188b67000000000d9", 0, "8d5fb5cb7575752ccaf31d42edad7cde28c74af5bcda4f7328022dd2bab2608b", 0xeb79},
    {"15130188b67000000000d9", 1, "46afdae5bababa96e5798ea176d6be6f1463a57b64fd91c0428620aeeb99d90c", 0xed93, 1}
  };
  for (const Fixture& fixture : fixtures) {
    const auto payload = hexBytes(fixture.payload);
    const auto captured = hexBytes(fixture.captured);
    uint8_t expected[22] = {};
    // Copy exactly the captured 169 frame bits at their observed alignment;
    // trailing capture noise is replaced with seven zero padding bits.
    for (unsigned int bit = 0; bit < 169; ++bit) {
      const unsigned int sourceBit = fixture.bitOffset + bit;
      if ((captured[sourceBit / 8] >> (7 - sourceBit % 8)) & 1U) {
        expected[bit / 8] |= static_cast<uint8_t>(1U << (7 - bit % 8));
      }
    }
    uint8_t encoded[22];
    tx_frame_detail::encode(AIR_ADDRESS, payload.data(), fixture.pid, encoded);
    assert(memcmp(encoded, expected, 22) == 0);
    const Decoded decoded = parse(encoded);
    assert(decoded.crc == fixture.crc && decoded.pid == fixture.pid && decoded.noAck == 1);
    assert(memcmp(decoded.payload, payload.data(), 11) == 0);
  }
}

void testLegacyModeIsExact()
{
  uint8_t originalA[22], originalB[22], a[22], b[22];
  for (uint8_t i = 0; i < 22; ++i) { originalA[i] = i; originalB[i] = static_cast<uint8_t>(255 - i); }
  assert(tx_frame_detail::buildPair(false, 999999, 17, 0, 0, 0, 0, originalA, originalB, a, b));
  assert(memcmp(a, originalA, 22) == 0 && memcmp(b, originalB, 22) == 0);
}

void testCounterWrapPidAndUnchangedApplicationBytes()
{
  const uint32_t elapsed[] = {0, 127, 128, 256, 384};
  const uint8_t counters[] = {0xfe, 0xfe, 0xff, 0x00, 0x01};
  for (unsigned int sample = 0; sample < 5; ++sample) {
    for (uint16_t queued : {uint16_t(0), uint16_t(1), uint16_t(2), uint16_t(3), uint16_t(4), uint16_t(65535)}) {
      uint8_t a[22], b[22];
      assert(tx_frame_detail::buildPair(true, elapsed[sample], queued, 0xfe, 128,
                                      AIR_ADDRESS, RIGHT_PAYLOAD, 0, 0, a, b));
      const Decoded first = parse(a), second = parse(b);
      assert(first.pid == (queued & 3) && second.pid == ((queued + 1) & 3));
      assert(first.noAck == 1 && second.noAck == 1);
      assert(first.payload[4] == counters[sample]);
      assert(memcmp(first.payload, second.payload, 11) == 0);
      for (unsigned int index = 0; index < 10; ++index) {
        if (index != 4) { assert(first.payload[index] == RIGHT_PAYLOAD[index]); }
      }
      assert(first.payload[6] == 0xae); // Preserve the observed application field.
      unsigned int checksum = 2;
      for (unsigned int index = 0; index < 10; ++index) { checksum += first.payload[index]; }
      assert(first.payload[10] == (checksum & 255));
    }
  }
}

void testPublicProfileAndInvalidInputs()
{
  uint8_t a[22], b[22], expectedA[22], expectedB[22];
  assert(buildTxPair(256, 6, a, b));
  assert(tx_frame_detail::buildPair(PROFILE_ADVANCE_COUNTERS, 256, 6,
      PROFILE_COUNTER_START, PROFILE_COUNTER_PERIOD_MS, PROFILE_AIR_ADDRESS,
      PROFILE_PAYLOAD, PROFILE_FRAME_A, PROFILE_FRAME_B, expectedA, expectedB));
  assert(memcmp(a, expectedA, 22) == 0 && memcmp(b, expectedB, 22) == 0);
  assert(!buildTxPair(0, 0, 0, b));
  assert(!tx_frame_detail::buildPair(true, 0, 0, 0, 0, AIR_ADDRESS, RIGHT_PAYLOAD, 0, 0, a, b));
  assert(!tx_frame_detail::buildPair(false, 0, 0, 0, 0, 0, 0, PROFILE_FRAME_A, PROFILE_FRAME_B, a, a));
}

void testBoomDownApplicationFieldPreserved()
{
  const auto downPayload = hexBytes("151301886d000072000092");
  for (uint32_t elapsed : {uint32_t(0), uint32_t(128)}) {
    uint8_t a[22], b[22];
    assert(tx_frame_detail::buildPair(true, elapsed, 0, 0x06, 128,
                                    AIR_ADDRESS, downPayload.data(), 0, 0, a, b));
    const Decoded first = parse(a), second = parse(b);
    assert(first.pid == 0 && second.pid == 1);
    assert(first.payload[4] == 0x06 + elapsed / 128);
    assert(first.payload[6] == 0 && first.payload[7] == 0x72);
    assert(memcmp(first.payload, second.payload, 11) == 0);
    for (unsigned int index = 0; index < 10; ++index) {
      if (index != 4) { assert(first.payload[index] == downPayload[index]); }
    }
    unsigned int checksum = 2;
    for (unsigned int index = 0; index < 10; ++index) { checksum += first.payload[index]; }
    assert(first.payload[10] == (checksum & 255));
  }
}

void testTrackApplicationFieldsPreserved()
{
  struct Track { const char* payload; uint8_t start; uint8_t command; };
  const Track tracks[] = {
    {"15130188b207000000006c", 0xc6, 0x07},
    {"15130188b67000000000d9", 0xce, 0x70}
  };
  for (const Track& track : tracks) {
    const auto payload = hexBytes(track.payload);
    for (uint32_t elapsed : {uint32_t(0), uint32_t(127), uint32_t(128), uint32_t(896)}) {
      for (uint16_t queued : {uint16_t(0), uint16_t(2), uint16_t(4), uint16_t(6)}) {
        uint8_t a[22], b[22];
        assert(tx_frame_detail::buildPair(true, elapsed, queued, track.start, 128,
                                        AIR_ADDRESS, payload.data(), 0, 0, a, b));
        const Decoded first = parse(a), second = parse(b);
        assert(first.pid == (queued & 3) && second.pid == ((queued + 1) & 3));
        assert(first.payload[4] == track.start + elapsed / 128);
        assert(first.payload[5] == track.command);
        for (unsigned int index = 6; index <= 9; ++index) { assert(first.payload[index] == 0); }
        for (unsigned int index = 0; index < 10; ++index) {
          if (index != 4) { assert(first.payload[index] == payload[index]); }
        }
        assert(memcmp(first.payload, second.payload, 11) == 0);
        unsigned int checksum = 2;
        for (unsigned int index = 0; index < 10; ++index) { checksum += first.payload[index]; }
        assert(first.payload[10] == (checksum & 255));
      }
    }
  }
}

int main()
{
  testCapturedFixtures();
  testLegacyModeIsExact();
  testCounterWrapPidAndUnchangedApplicationBytes();
  testPublicProfileAndInvalidInputs();
  testBoomDownApplicationFieldPreserved();
  testTrackApplicationFieldsPreserved();
  std::cout << "TX encoding tests passed (6 scenarios).\n";
}
