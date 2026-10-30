// Node 1 (F1) mantık testi: gerçek firmware + taklit donanım
#include "mock/Arduino.h"
#include "mock/RF24.h"
#include "mock/DHT.h"
#include "../node1_sensor/src/main.cpp"
#include "mock/impl.inc"
#include <cstdlib>

static float mqPin(uint32_t t) {
  float base = 150.0f + 250.0f * expf(-(float)t / 15000.0f);           // ısınma 400 → 150 mV
  float gas = 0.0f;
  if (t >= 60000) {
    if (t < 63000) gas = 600.0f * (1.0f - expf(-(float)(t - 60000) / 400.0f));
    else gas = 600.0f * (1.0f - expf(-3000.0f / 400.0f)) * expf(-(float)(t - 63000) / 8000.0f);
  }
  return base + gas;
}
static float tempAt(uint32_t t) {
  if (t < 70000) return 24.0f;
  if (t < 78000) return 24.0f + 12.0f * (float)(t - 70000) / 8000.0f;
  if (t < 85000) return 36.0f;
  return 24.0f + 12.0f * expf(-(float)(t - 85000) / 15000.0f);
}
static int fails = 0;
static void check(bool ok, const char* what) { printf("%s  %s\n", ok ? "PASS" : "FAIL", what); if (!ok) fails++; }
static bool has(const std::string& s, const char* sub) { return s.find(sub) != std::string::npos; }
static long field(const std::string& s, const char* key) {
  std::string k = std::string("\"") + key + "\":"; size_t p = s.find(k); if (p == std::string::npos) return -9999;
  return strtol(s.c_str() + p + k.size(), nullptr, 10);
}

int main(int argc, char**) {
  g_verbose = argc > 1;
  radio.ackState = {1, 0, 0, 1};
  g_mq_pin_mv = mqPin(0);
  setup();
  while (g_now < 170000) {
    const uint32_t t = g_now;
    g_mq_pin_mv = mqPin(t);
    g_temp = tempAt(t);
    g_hum = 45.0f - (g_temp - 24.0f) * 1.2f;
    radio.link = !(t >= 100000 && t < 106000);
    if (t >= 66000) radio.ackState = {1, 1, 1, 1};     // merkez: SUSTUR
    if (t >= 125000) radio.ackState = {1, 2, 2, 1};    // merkez: TEST BİP
    g_btn = (t >= 140000 && t < 141200) ? LOW : HIGH;
    loop();
  }
  // --- radyo kaydını node2 testi için yaz ---
  FILE* f = fopen("node1_radio.txt", "w");
  for (size_t i = 0; i < radio.txlog.size(); i++) {
    fprintf(f, "%u %d ", radio.txlog[i].first, radio.txok[i] ? 1 : 0);
    for (uint8_t b : radio.txlog[i].second) fprintf(f, "%02x", b);
    fprintf(f, "\n");
  }
  fclose(f);
  FILE* g = fopen("node1_usb.jsonl", "w");
  for (auto& l : Serial.out) fprintf(g, "%s\n", l.second.c_str());
  fclose(g);

  // --- kontroller ---
  uint32_t firstAlarm = 0, firstHeat = 0, firstSilenced = 0, firstClear = 0, testAt = 0;
  bool alarmBefore60 = false, eventAtOnset = false, rfFailSeen = false, rfRecovered = false;
  long maxTxf = 0;
  for (auto& [t, l] : Serial.out) {
    if (!has(l, "\"type\":\"tel\"")) continue;
    long a = field(l, "alarm"), fl = field(l, "flags");
    if (a > 0 && t < 60000) alarmBefore60 = true;
    if (a > 0 && !firstAlarm) { firstAlarm = t; eventAtOnset = has(l, "\"kind\":\"event\""); }
    if ((a & 2) && !firstHeat) firstHeat = t;
    if ((fl & 4) && !firstSilenced) firstSilenced = t;
    if (firstAlarm && a == 0 && !firstClear && t > firstAlarm) firstClear = t;
    if ((fl & 8) && a == 3 && !testAt) testAt = t;
    if (t >= 100000 && t < 106000 && has(l, "\"rf_ok\":0")) rfFailSeen = true;
    if (t >= 106000 && t < 108000 && has(l, "\"rf_ok\":1")) rfRecovered = true;
    long tx = field(l, "txf"); if (tx > maxTxf) maxTxf = tx;
  }
  int buzzOnAfterSilence = 0, buzzDuringAlarm = 0, chirpAfterTest = 0;
  for (auto& [t, v] : g_buzzer_edges) {
    if (v == HIGH && t >= 60000 && t < 66000) buzzDuringAlarm++;
    if (v == HIGH && firstSilenced && t > firstSilenced + 5 && firstClear && t < firstClear) buzzOnAfterSilence++;
    if (v == HIGH && t >= 125000 && t < 127000) chirpAfterTest++;
  }
  printf("ilk alarm %.3f s | ilk ısı %.3f s | susturma %.3f s | alarm bitti %.3f s | test %.3f s | max txf %ld\n",
         firstAlarm / 1e3, firstHeat / 1e3, firstSilenced / 1e3, firstClear / 1e3, testAt / 1e3, maxTxf);
  check(!alarmBefore60, "ısınma ve normal dönemde yanlış alarm yok");
  check(firstAlarm >= 60000 && firstAlarm <= 61500, "gaz alarmı çakmaktan sonra <=1.5 s içinde");
  check(eventAtOnset, "alarm başlangıcı 'event' paketiyle anında gönderildi");
  check(buzzDuringAlarm >= 5, "alarm sırasında buzzer öttü");
  check(firstSilenced >= 66000 && firstSilenced <= 66400, "merkezden SUSTUR komutu <=0.4 s içinde uygulandı");
  check(buzzOnAfterSilence == 0, "susturulduktan sonra alarm bitene kadar buzzer sessiz");
  check(firstHeat >= 72000 && firstHeat <= 77000, "ısı alarmı sıcaklık artışıyla geldi");
  check(firstClear > 80000 && firstClear < 115000, "alarm koşul bitince temizlendi");
  check(rfFailSeen && rfRecovered && maxTxf > 0, "radyo kesintisi rf_ok/txf ile raporlandı ve toparlandı");
  check(chirpAfterTest >= 3, "merkezden TEST BİP komutu ile 3 bip");
  check(testAt >= 140000 && testAt <= 141500, "BOOT butonu test alarmı (alarm=3, flags&8)");
  printf("%s (%d hata) — %zu seri satır, %zu radyo paketi\n", fails ? "BAŞARISIZ" : "TÜMÜ GEÇTİ", fails, Serial.out.size(), radio.txlog.size());
  return fails ? 1 : 0;
}
