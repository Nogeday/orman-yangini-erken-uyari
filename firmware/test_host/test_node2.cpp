// Node 2 (F2 köprü) mantık testi: F1'in gerçek radyo çıktısı + bilgisayar komutları
#include "mock/Arduino.h"
#include "mock/RF24.h"
#include "mock/DHT.h"
#include "../node2_gateway/src/main.cpp"
#include "mock/impl.inc"
#include <cstdlib>
#include <fstream>
#include <sstream>

struct Air { uint32_t t; bool ok; std::vector<uint8_t> b; };
static int fails = 0;
static void check(bool ok, const char* what) { printf("%s  %s\n", ok ? "PASS" : "FAIL", what); if (!ok) fails++; }
static bool has(const std::string& s, const char* sub) { return s.find(sub) != std::string::npos; }
static long field(const std::string& s, const char* key) {
  std::string k = std::string("\"") + key + "\":"; size_t p = s.find(k); if (p == std::string::npos) return -9999;
  return strtol(s.c_str() + p + k.size(), nullptr, 10);
}

int main() {
  std::vector<Air> air;
  std::ifstream in("node1_radio.txt");
  std::string line;
  while (std::getline(in, line)) {
    std::istringstream ss(line); Air a; int ok; std::string hex; ss >> a.t >> ok >> hex; a.ok = ok;
    for (size_t i = 0; i + 1 < hex.size(); i += 2) a.b.push_back((uint8_t)strtol(hex.substr(i, 2).c_str(), nullptr, 16));
    air.push_back(a);
  }
  // F1, F2'den 2.0 s sonra açılsın (gerçekçi: önce köprü takılır)
  const uint32_t OFFSET = 2000;
  size_t next = 0; int delivered = 0; bool dupInjected = false;
  struct Cmd { uint32_t t; const char* s; } cmds[] = {
    {68000, "silence 1\n"}, {127000, "test 1\n"}, {132000, "status\n"}, {133000, "silence \"x\n"},
    {134000, "HELP\n"}, {135000, "ping\n"} };
  size_t ci = 0;
  g_mq_pin_mv = 160.0f; g_temp = 23.0f; g_hum = 48.0f;
  setup();
  std::vector<uint8_t> ackAfterSilence, ackAfterTest;
  while (g_now < 175000) {
    while (next < air.size() && air[next].t + OFFSET <= g_now) {
      if (air[next].ok) {
        radio.rxq.push_back(air[next].b); delivered++;
        if (!dupInjected && air[next].t > 30000) { radio.rxq.push_back(air[next].b); dupInjected = true; }
      }
      next++;
    }
    while (ci < sizeof(cmds) / sizeof(cmds[0]) && cmds[ci].t <= g_now) { Serial.feed(cmds[ci].s); ci++; }
    loop();
    if (g_now == 68100) ackAfterSilence = radio.lastAckWritten;
    if (g_now == 127100) ackAfterTest = radio.lastAckWritten;
  }
  FILE* f = fopen("gateway_out.tsv", "w");
  for (auto& l : Serial.out) fprintf(f, "%u\t%s\n", l.first, l.second.c_str());
  fclose(f);

  int rfLines = 0, lostAt = 0, okAt = 0, chirpEdges = 0, minLq = 101, firstAlarmLine = 0;
  for (auto& [t, l] : Serial.out) {
    if (has(l, "\"via\":\"rf\"")) {
      rfLines++;
      long lq = field(l, "lq"); if (lq >= 0 && lq < minLq) minLq = (int)lq;
      if (!firstAlarmLine && field(l, "alarm") > 0) firstAlarmLine = (int)t;
    }
    if (has(l, "\"type\":\"link\"") && has(l, "\"lost\"") && !lostAt) lostAt = (int)t;
    if (has(l, "\"type\":\"link\"") && has(l, "\"state\":\"ok\"") && !okAt) okAt = (int)t;
  }
  for (auto& [t, v] : g_buzzer_edges) if (v == HIGH && firstAlarmLine && t >= (uint32_t)firstAlarmLine && t < (uint32_t)firstAlarmLine + 1000) chirpEdges++;
  bool badCmdSanitized = false;
  for (auto& [t, l] : Serial.out) if (has(l, "\"type\":\"cmd\"") && has(l, "\"target\":\"?x\"")) badCmdSanitized = true;
  printf("teslim edilen F1 paketi %d | rf satırı %d | ilk alarm satırı %.3f s | link lost %.3f s | link ok %.3f s | min lq %d\n",
         delivered, rfLines, firstAlarmLine / 1e3, lostAt / 1e3, okAt / 1e3, minLq);
  check(rfLines == delivered, "her teslim edilen F1 paketi tam 1 JSON satırı (tekrar paketi elendi)");
  check(dupInjected, "tekrar paketi testi yapıldı");
  check(firstAlarmLine >= 62000 && firstAlarmLine <= 62400, "F1 alarmı köprüden ~anında çıktı");
  check(chirpEdges == 2, "köprü F1 alarmında 2 kısa bip çaldı (kablosuz teslim sesi)");
  check(lostAt >= 104500 && lostAt <= 106000, "F1 sessizliğinde 3.5 s sonra link lost");
  check(okAt >= 108000 && okAt <= 108400, "F1 dönünce link ok");
  check(minLq < 100, "kayıp paketler bağlantı kalitesine yansıdı");
  check(ackAfterSilence.size() == 4 && ackAfterSilence[1] == 1 && ackAfterSilence[2] == 1, "silence 1 → ACK payload (cmdSeq=1, SILENCE)");
  check(ackAfterTest.size() == 4 && ackAfterTest[1] == 2 && ackAfterTest[2] == 2, "test 1 → ACK payload (cmdSeq=2, TEST_BEEP)");
  check(badCmdSanitized, "bozuk komut güvenli biçimde yanıtlandı");
  printf("%s (%d hata) — %zu satır\n", fails ? "BAŞARISIZ" : "TÜMÜ GEÇTİ", fails, Serial.out.size());
  return fails ? 1 : 0;
}
