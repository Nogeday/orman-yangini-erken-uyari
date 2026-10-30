/*
 * =============================================================================
 *  ORMAN YANGINI ERKEN UYARI MESH AĞI  ·  NODE 2 (F2)  ·  KÖPRÜ / GATEWAY
 * =============================================================================
 *  Kart      : ESP32 DEVKIT V1 (DOIT, 30 pin)
 *  Görev     : 1) Node 1'den (F1) nRF24L01+PA+LNA ile gelen paketleri dinler ve
 *                 HİÇ BEKLETMEDEN USB seri porttan tek satır JSON olarak
 *                 bilgisayardaki simülasyona aktarır.
 *              2) Kendi DHT11 + MQ + buzzer'ını yönetir (via:"local").
 *              3) Bilgisayardan gelen komutları (silence/test) ACK paketinin
 *                 içine koyarak F1'e geri iletir (çift yönlü bağlantı).
 *              4) F1'den 3.5 s veri gelmezse "link lost" olayı üretir.
 *  Güç       : USB (bilgisayar). Bu karta PİL BAĞLAMAYIN (bkz. docs/HARDWARE.md)
 *  Protokol  : docs/PROTOCOL.md — paket yapısı Node 1 ile BİREBİR aynı olmalı
 *  Kütüphane : RF24 (TMRh20) ≥ 1.4.8 · DHT sensor library (Adafruit) ≥ 1.4.6
 *              · Adafruit Unified Sensor
 * -----------------------------------------------------------------------------
 *  BAĞLANTI (Node 1 ile aynı; DevKit V1 yazıları: D4, D5, D18 ...)
 *    nRF24L01+PA+LNA : VCC→3V3 (+47µF elektrolitik ve 100nF, modül pinlerinde)
 *                      GND→GND  CE→D4  CSN→D5  SCK→D18  MOSI→D23  MISO→D19  IRQ→boş
 *    DHT11           : VCC→3V3  GND→GND  DATA→D27
 *    MQ gaz modülü   : VCC→VIN (USB 5V)  GND→GND  AO→[10k]→D34→[10k]→GND  DO→boş
 *    Buzzer          : sinyal→D25         Durum LED'i: D2 (kart üstü)
 * -----------------------------------------------------------------------------
 *  SERİ KOMUTLAR (115200 baud, satır sonu \n):
 *    silence 1 | silence 2 | silence all   → buzzer'ı sustur (alarm bitene kadar)
 *    test 1    | test 2                    → 3 kısa bip (bağlantı testi)
 *    ping | status | help
 * =============================================================================
 */
#include <Arduino.h>
#include <SPI.h>
#include <RF24.h>
#include <DHT.h>
#include <math.h>
#include <string.h>
#include <ctype.h>

// ============================================================================
//  1) AYARLAR
// ============================================================================
#define NODE_ID             2
#define REMOTE_NODE_ID      1       // radyodan dinlenen saha düğümü (F1)
#define FW_VERSION          "1.0.0"
#define SERIAL_BAUD         115200

// ---- Pinler (GPIO numarası; Node 1 ile aynı) ----
#define PIN_NRF_CE          4
#define PIN_NRF_CSN         5
#define PIN_DHT             27
#define PIN_MQ_AO           34
#define PIN_BUZZER          25
#define PIN_LED             2
#define PIN_TEST_BTN        0
#define PIN_VBAT            35

// ---- Buzzer ----
#define BUZZER_IS_ACTIVE    1       // 1: aktif buzzer · 0: pasif buzzer
#define BUZZER_ON_LEVEL     HIGH    // "low level trigger" modülde LOW
#define BUZZER_TONE_HZ      2700
#define RELAY_CHIRP         1       // F1 alarmı radyodan gelince köprü 2 kısa bip çalar (kablosuz teslimin sesli kanıtı)

// ---- MQ gaz sensörü ----
#define MQ_DIVIDER_RATIO    2.0f
#define MQ_WARMUP_MS        45000UL
#define GAS_DELTA_MV        350.0f
#define GAS_RATIO           1.40f
#define GAS_CLEAR_HOLD_MS   4000UL

// ---- Sıcaklık ----
#define TEMP_ALARM_C        45.0f
#define TEMP_RISE_C         6.0f
#define HEAT_CLEAR_HOLD_MS  6000UL
#define DHT_STALE_MS        10000UL

// ---- Zamanlama ----
#define MQ_SAMPLE_MS        100UL
#define DHT_READ_MS         2100UL
#define TX_PERIOD_MS        1000UL  // köprünün kendi telemetrisi
#define TX_PERIOD_ALARM_MS  300UL
#define RADIO_RETRY_MS      5000UL
#define RADIO_HEALTH_MS     5000UL  // nRF24 modülünü periyodik yoklama
#define LINK_TIMEOUT_MS     3500UL  // F1'den bu süre paket gelmezse "lost"
#define STATUS_PERIOD_MS    5000UL  // "gw" durum satırı aralığı

// ---- Test butonu (USB'ye bağlı kartta kapalı: seri port hatları GPIO0'ı etkileyebilir) ----
#define ENABLE_TEST_BUTTON  0
#define TEST_BTN_HOLD_MS    800UL
#define TEST_ALARM_MS       10000UL

// ---- Radyo (Node 1 ile AYNI olmalı) ----
#define RF_CHANNEL          78
#define RF_DATA_RATE        RF24_1MBPS
#define RF_PA_LEVEL         RF24_PA_LOW
#define RF_SPI_HZ           4000000UL
#define RF_RETRY_DELAY      5
#define RF_RETRY_COUNT      15
static const uint8_t RF_ADDR[6] = "OYMA1";

// ---- Güç ----
#define ENABLE_VBAT         0
#define VBAT_DIVIDER_RATIO  3.128f

// ============================================================================
//  2) PROTOKOL — node1_sensor/src/main.cpp ile BİREBİR AYNI (docs/PROTOCOL.md)
// ============================================================================
#define PROTO_VERSION 1

enum : uint8_t { PKT_TELEMETRY = 0, PKT_EVENT = 1, PKT_BOOT = 2 };
enum : uint8_t { ALARM_GAS = 0x01, ALARM_HEAT = 0x02 };
enum : uint8_t { FLAG_WARMUP = 0x01, FLAG_DHT_ERR = 0x02, FLAG_SILENCED = 0x04, FLAG_TEST = 0x08 };
enum : uint8_t { CMD_NONE = 0, CMD_SILENCE = 1, CMD_TEST_BEEP = 2 };

#define TEMP_INVALID  ((int16_t)-32768)
#define HUM_INVALID   ((uint16_t)0xFFFF)

struct __attribute__((packed)) SensorPacket {
  uint8_t  proto;
  uint8_t  nodeId;
  uint8_t  type;
  uint8_t  alarm;
  uint8_t  flags;
  uint8_t  txFail;
  uint16_t seq;
  uint32_t uptimeMs;
  int16_t  tempX10;
  uint16_t humX10;
  uint16_t gasMv;
  uint16_t gasBaseMv;
  uint16_t vbatMv;
};
static_assert(sizeof(SensorPacket) == 22, "SensorPacket 22 bayt olmali");

struct __attribute__((packed)) AckPayload {
  uint8_t proto;
  uint8_t cmdSeq;
  uint8_t cmd;
  uint8_t target;
};
static_assert(sizeof(AckPayload) == 4, "AckPayload 4 bayt olmali");

// ============================================================================
//  3) GLOBAL DURUM
// ============================================================================
static RF24 radio(PIN_NRF_CE, PIN_NRF_CSN, RF_SPI_HZ);
static DHT  dht(PIN_DHT, DHT11);

static bool     radioOk          = false;
static uint32_t radioRetryAtMs   = 0;
static uint32_t radioHealthAtMs  = 0;

// --- köprünün kendi sensörleri (Node 1 ile aynı mantık) ---
static float    gasMv            = 0.0f;
static float    gasBaseMv        = 0.0f;
static bool     gasInit          = false;
static float    tempC            = NAN;
static float    humPct           = NAN;
static float    tempBaseC        = NAN;
static bool     dhtAttempted     = false;
static bool     dhtEverOk        = false;
static uint32_t lastDhtOkMs      = 0;

static uint8_t  realAlarm        = 0;
static uint8_t  alarmBits        = 0;
static bool     gasClearing      = false;
static uint32_t gasClearSinceMs  = 0;
static bool     heatClearing     = false;
static uint32_t heatClearSinceMs = 0;
static bool     testActive       = false;
static uint32_t testUntilMs      = 0;
static bool     silenced         = false;

#if ENABLE_TEST_BUTTON
static bool     btnDown          = false;
static bool     btnFired         = false;
static uint32_t btnDownSinceMs   = 0;
#endif

static uint16_t txSeq            = 0;
static uint32_t lastTxMs         = 0;
static uint32_t lastMqMs         = 0;
static uint32_t lastDhtMs        = 0;
static uint32_t lastStatusMs     = 0;

// --- F1 bağlantı istatistikleri ---
struct LinkStats {
  bool     seen;
  bool     lost;
  uint16_t lastSeq;
  uint32_t lastUptimeMs;
  uint32_t lastRxMs;
  uint32_t rxCount;
  uint32_t lostCount;
  float    winRx;      // kayan pencere: alınan
  float    winExp;     // kayan pencere: beklenen
  uint8_t  lastAlarm;
};
static LinkStats link1 = { false, false, 0, 0, 0, 0, 0, 0.0f, 0.0f, 0 };
static uint32_t lastRxBlinkMs = 0;

// --- F1'e ACK içinde giden komut durumu ---
static AckPayload ackState = { PROTO_VERSION, 0, CMD_NONE, REMOTE_NODE_ID };

// --- seri komut tamponu ---
static char    cmdBuf[64];
static uint8_t cmdLen = 0;
static bool    cmdOverflow = false;

// ============================================================================
//  4) YARDIMCILAR (Node 1 ile aynı)
// ============================================================================
static inline bool isWarmingUp(uint32_t now) { return now < MQ_WARMUP_MS; }

static inline bool dhtFresh(uint32_t now) {
  return dhtEverOk && (now - lastDhtOkMs) < DHT_STALE_MS;
}

static void logJson(const char* level, const char* msg) {
  char buf[160];
  snprintf(buf, sizeof(buf), "{\"type\":\"log\",\"node\":%d,\"level\":\"%s\",\"msg\":\"%s\"}",
           NODE_ID, level, msg);
  Serial.println(buf);
}

static void fmtX10(char* out, size_t n, int32_t v, bool valid) {
  if (!valid) { snprintf(out, n, "null"); return; }
  const bool neg = v < 0;
  const int32_t a = neg ? -v : v;
  snprintf(out, n, "%s%ld.%ld", neg ? "-" : "", (long)(a / 10), (long)(a % 10));
}

static const char* kindName(uint8_t type) {
  switch (type) {
    case PKT_EVENT: return "event";
    case PKT_BOOT:  return "boot";
    default:        return "periodic";
  }
}

static void printPacketJson(const SensorPacket& p, const char* via, int lq, int rfOk) {
  char t[12];
  char h[12];
  fmtX10(t, sizeof(t), p.tempX10, p.tempX10 != TEMP_INVALID);
  fmtX10(h, sizeof(h), p.humX10, p.humX10 != HUM_INVALID);

  char buf[320];
  int n = snprintf(buf, sizeof(buf),
                   "{\"type\":\"tel\",\"node\":%u,\"via\":\"%s\",\"kind\":\"%s\",\"seq\":%u,\"up\":%lu,"
                   "\"t\":%s,\"h\":%s,\"gas\":%u,\"base\":%u,\"alarm\":%u,\"flags\":%u,\"vbat\":%u,\"txf\":%u",
                   (unsigned)p.nodeId, via, kindName(p.type), (unsigned)p.seq, (unsigned long)p.uptimeMs,
                   t, h, (unsigned)p.gasMv, (unsigned)p.gasBaseMv, (unsigned)p.alarm, (unsigned)p.flags,
                   (unsigned)p.vbatMv, (unsigned)p.txFail);
  if (n < 0 || n >= (int)sizeof(buf)) return;
  if (lq >= 0) {
    n += snprintf(buf + n, sizeof(buf) - n, ",\"lq\":%d", lq);
    if (n >= (int)sizeof(buf)) return;
  }
  if (rfOk >= 0) {
    n += snprintf(buf + n, sizeof(buf) - n, ",\"rf_ok\":%d", rfOk);
    if (n >= (int)sizeof(buf)) return;
  }
  snprintf(buf + n, sizeof(buf) - n, "}");
  Serial.println(buf);
}

static int linkQualityPct() {
  if (link1.winExp <= 0.0f) return -1;
  int q = (int)lroundf(100.0f * link1.winRx / link1.winExp);
  return q > 100 ? 100 : q;
}

// ============================================================================
//  5) BUZZER ve LED
// ============================================================================
static bool     buzzerIsOn   = false;
static uint8_t  chirpLeft    = 0;
static bool     chirpPhaseOn = false;
static uint32_t chirpNextMs  = 0;

static void buzzerWrite(bool on) {
  if (on == buzzerIsOn) return;
  buzzerIsOn = on;
#if BUZZER_IS_ACTIVE
  digitalWrite(PIN_BUZZER, on ? BUZZER_ON_LEVEL : !BUZZER_ON_LEVEL);
#else
  if (on) tone(PIN_BUZZER, BUZZER_TONE_HZ);
  else    noTone(PIN_BUZZER);
#endif
}

static void buzzerChirp(uint8_t count) {
  chirpLeft    = count;
  chirpPhaseOn = false;
  chirpNextMs  = millis();
}

static void buzzerUpdate(uint32_t now) {
  if (alarmBits != 0 && !silenced) {
    buzzerWrite((now % 300UL) < 180UL);
    chirpLeft = 0;
    return;
  }
  if (chirpLeft > 0) {
    if ((int32_t)(now - chirpNextMs) >= 0) {
      chirpPhaseOn = !chirpPhaseOn;
      buzzerWrite(chirpPhaseOn);
      chirpNextMs = now + (chirpPhaseOn ? 70UL : 110UL);
      if (!chirpPhaseOn) chirpLeft--;
    }
    return;
  }
  buzzerWrite(false);
}

static void ledUpdate(uint32_t now) {
  bool on;
  if (alarmBits != 0) {
    on = (now % 200UL) < 100UL;                              // kendi alarmı: hızlı
  } else if (link1.lastAlarm != 0 && !link1.lost) {
    on = (now % 600UL) < 300UL;                              // F1 alarmda: orta hız
  } else if (!radioOk) {
    const uint32_t ph = now % 1000UL;
    on = ph < 80UL || (ph >= 200UL && ph < 280UL);           // radyo yok: çift flaş
  } else {
    on = (now - lastRxBlinkMs) < 40UL;                       // F1 paketi geldi: kısa flaş
  }
  digitalWrite(PIN_LED, on ? HIGH : LOW);
}

// ============================================================================
//  6) SENSÖRLER (Node 1 ile aynı)
// ============================================================================
static float readMqPinMv() {
  uint32_t sum = 0;
  for (uint8_t i = 0; i < 8; i++) sum += analogReadMilliVolts(PIN_MQ_AO);
  return (float)sum / 8.0f;
}

static void sampleGas(uint32_t now) {
  const float mv = readMqPinMv() * MQ_DIVIDER_RATIO;
  if (!gasInit) {
    gasMv = mv;
    gasBaseMv = mv;
    gasInit = true;
    return;
  }
  gasMv = gasMv * 0.5f + mv * 0.5f;
  if (isWarmingUp(now)) {
    gasBaseMv += (gasMv - gasBaseMv) * 0.05f;
  } else if (!(realAlarm & ALARM_GAS) && gasMv < gasBaseMv + GAS_DELTA_MV * 0.5f) {
    gasBaseMv += (gasMv - gasBaseMv) * 0.002f;
  }
}

static void readDht(uint32_t now) {
  dhtAttempted = true;
  const float t = dht.readTemperature();
  const float h = dht.readHumidity();
  if (isnan(t) || isnan(h)) return;
  tempC = t;
  humPct = h;
  dhtEverOk = true;
  lastDhtOkMs = now;
  if (isnan(tempBaseC)) {
    tempBaseC = t;
  } else if (!(realAlarm & ALARM_HEAT) && (t - tempBaseC) < TEMP_RISE_C * 0.5f) {
    tempBaseC += (t - tempBaseC) * 0.05f;
  }
}

static uint16_t readVbatMv() {
#if ENABLE_VBAT
  uint32_t sum = 0;
  for (uint8_t i = 0; i < 8; i++) sum += analogReadMilliVolts(PIN_VBAT);
  return (uint16_t)lroundf(((float)sum / 8.0f) * VBAT_DIVIDER_RATIO);
#else
  return 0;
#endif
}

// ============================================================================
//  7) ALARM MANTIĞI (Node 1 ile aynı)
// ============================================================================
static void evaluateAlarms(uint32_t now) {
  uint8_t next = realAlarm;

  const float gasThr = fmaxf(gasBaseMv + GAS_DELTA_MV, gasBaseMv * GAS_RATIO);
  if (gasInit && !isWarmingUp(now) && gasMv >= gasThr) {
    next |= ALARM_GAS;
    gasClearing = false;
  } else if (next & ALARM_GAS) {
    if (gasMv < gasBaseMv + GAS_DELTA_MV * 0.5f) {
      if (!gasClearing) {
        gasClearing = true;
        gasClearSinceMs = now;
      } else if (now - gasClearSinceMs >= GAS_CLEAR_HOLD_MS) {
        next = (uint8_t)(next & ~ALARM_GAS);
        gasClearing = false;
      }
    } else {
      gasClearing = false;
    }
  }

  if (dhtFresh(now) && !isnan(tempBaseC)) {
    const float rise = tempC - tempBaseC;
    if (tempC >= TEMP_ALARM_C || rise >= TEMP_RISE_C) {
      next |= ALARM_HEAT;
      heatClearing = false;
    } else if (next & ALARM_HEAT) {
      if (tempC < TEMP_ALARM_C - 2.0f && rise < TEMP_RISE_C - 2.0f) {
        if (!heatClearing) {
          heatClearing = true;
          heatClearSinceMs = now;
        } else if (now - heatClearSinceMs >= HEAT_CLEAR_HOLD_MS) {
          next = (uint8_t)(next & ~ALARM_HEAT);
          heatClearing = false;
        }
      } else {
        heatClearing = false;
      }
    }
  } else if (!dhtFresh(now)) {
    next = (uint8_t)(next & ~ALARM_HEAT);
    heatClearing = false;
  }
  realAlarm = next;

  if (testActive && (int32_t)(now - testUntilMs) >= 0) testActive = false;
  alarmBits = (uint8_t)(realAlarm | (testActive ? (ALARM_GAS | ALARM_HEAT) : 0));
}

static uint8_t currentFlags(uint32_t now) {
  uint8_t f = 0;
  if (isWarmingUp(now))                  f |= FLAG_WARMUP;
  if (dhtAttempted && !dhtFresh(now))    f |= FLAG_DHT_ERR;
  if (silenced)                          f |= FLAG_SILENCED;
  if (testActive)                        f |= FLAG_TEST;
  return f;
}

static void updateTestButton(uint32_t now) {
#if ENABLE_TEST_BUTTON
  const bool down = digitalRead(PIN_TEST_BTN) == LOW;
  if (down && !btnDown) {
    btnDown = true;
    btnFired = false;
    btnDownSinceMs = now;
  } else if (!down && btnDown) {
    btnDown = false;
  }
  if (btnDown && !btnFired && (now - btnDownSinceMs) >= TEST_BTN_HOLD_MS) {
    btnFired = true;
    testActive = true;
    testUntilMs = now + TEST_ALARM_MS;
    logJson("info", "BOOT butonu: test alarmi baslatildi");
  }
#else
  (void)now;
#endif
}

// ============================================================================
//  8) KÖPRÜNÜN KENDİ TELEMETRİSİ
// ============================================================================
static void sendLocalTelemetry(uint8_t type, uint32_t now) {
  SensorPacket p;
  memset(&p, 0, sizeof(p));
  p.proto     = PROTO_VERSION;
  p.nodeId    = NODE_ID;
  p.type      = type;
  p.alarm     = alarmBits;
  p.flags     = currentFlags(now);
  p.txFail    = 0;
  p.seq       = txSeq++;
  p.uptimeMs  = now;
  p.tempX10   = dhtFresh(now) ? (int16_t)lroundf(tempC * 10.0f) : TEMP_INVALID;
  p.humX10    = dhtFresh(now) ? (uint16_t)lroundf(humPct * 10.0f) : HUM_INVALID;
  p.gasMv     = (uint16_t)constrain(lroundf(gasMv), 0L, 65534L);
  p.gasBaseMv = (uint16_t)constrain(lroundf(gasBaseMv), 0L, 65534L);
  p.vbatMv    = readVbatMv();
  lastTxMs = now;
  printPacketJson(p, "local", -1, -1);
}

// ============================================================================
//  9) RADYO (alıcı + ACK payload ile geri kanal)
// ============================================================================
static void reloadAckPayload() {
  if (!radioOk) return;
  radio.flush_tx();                                          // FIFO'da daima TEK güncel durum kalsın
  radio.writeAckPayload(1, &ackState, sizeof(ackState));
}

static bool radioInit() {
  if (!radio.begin()) return false;
  if (!radio.isChipConnected()) return false;
  radio.setChannel(RF_CHANNEL);
  radio.setDataRate(RF_DATA_RATE);
  radio.setPALevel(RF_PA_LEVEL);
  radio.setCRCLength(RF24_CRC_16);
  radio.setAutoAck(true);
  radio.setRetries(RF_RETRY_DELAY, RF_RETRY_COUNT);
  radio.enableDynamicPayloads();
  radio.enableAckPayload();
  radio.openReadingPipe(1, RF_ADDR);
  radio.startListening();
  radio.flush_tx();
  radio.writeAckPayload(1, &ackState, sizeof(ackState));
  return true;
}

static void printLinkEvent(const char* state, uint32_t ageMs) {
  char buf[128];
  snprintf(buf, sizeof(buf), "{\"type\":\"link\",\"node\":%d,\"state\":\"%s\",\"age_ms\":%lu}",
           REMOTE_NODE_ID, state, (unsigned long)ageMs);
  Serial.println(buf);
}

static void handleRemotePacket(const SensorPacket& p, uint32_t now) {
  if (p.nodeId != REMOTE_NODE_ID) {
    logJson("warn", "bilinmeyen node kimligi, paket yok sayildi");
    return;
  }

  // --- sıra numarası: tekrar / kayıp / yeniden başlama ---
  if (link1.seen && p.seq == link1.lastSeq && p.uptimeMs == link1.lastUptimeMs) {
    return;                                                  // aynı paket tekrar geldi (ACK kaybı) → yok say
  }
  const bool restarted = !link1.seen || p.type == PKT_BOOT || p.uptimeMs < link1.lastUptimeMs;
  if (restarted) {
    link1.winRx = 0.0f;
    link1.winExp = 1.0f;
  } else {
    const uint16_t gap = (uint16_t)(p.seq - link1.lastSeq);
    if (gap == 0) return;                                    // aynı sıra numarası → yok say
    if (gap > 1000) {                                        // mantıksız sıçrama → sayaçları sıfırla
      link1.winRx = 0.0f;
      link1.winExp = 1.0f;
    } else {
      link1.lostCount += (uint32_t)(gap - 1);
      link1.winExp += (float)gap;
    }
  }
  link1.winRx += 1.0f;
  if (link1.winExp > 60.0f) {                                // pencereyi yaşlandır
    link1.winRx *= 0.5f;
    link1.winExp *= 0.5f;
  }
  if (link1.winRx > link1.winExp) link1.winRx = link1.winExp;

  if (link1.lost) {
    link1.lost = false;
    printLinkEvent("ok", now - link1.lastRxMs);
  }

  const uint8_t prevAlarm = link1.lastAlarm;
  link1.seen         = true;
  link1.lastSeq      = p.seq;
  link1.lastUptimeMs = p.uptimeMs;
  link1.lastRxMs     = now;
  link1.lastAlarm    = p.alarm;
  link1.rxCount++;
  lastRxBlinkMs = now;

  // ÖNCE simülasyona yaz (gecikmesiz), sonra yerel tepkiler
  printPacketJson(p, "rf", linkQualityPct(), -1);

#if RELAY_CHIRP
  if (prevAlarm == 0 && p.alarm != 0 && alarmBits == 0) buzzerChirp(2);
#endif
}

static void pollRadio(uint32_t now) {
  if (!radioOk) return;
  bool gotAny = false;
  uint8_t guard = 0;
  uint8_t pipe = 0;
  while (radio.available(&pipe) && guard++ < 6) {
    const uint8_t len = radio.getDynamicPayloadSize();
    if (len == 0) continue;                                  // bozuk boyut: kütüphane RX FIFO'yu boşalttı
    uint8_t buf[32];
    radio.read(buf, len);
    gotAny = true;
    if (len != sizeof(SensorPacket)) {
      logJson("warn", "beklenmeyen paket boyu, yok sayildi");
      continue;
    }
    SensorPacket p;
    memcpy(&p, buf, sizeof(p));
    if (p.proto != PROTO_VERSION) {
      logJson("warn", "protokol surumu uyusmuyor (iki karta ayni firmware surumunu yukleyin)");
      continue;
    }
    handleRemotePacket(p, now);
  }
  if (gotAny) reloadAckPayload();
}

static void checkLink(uint32_t now) {
  if (link1.seen && !link1.lost && (now - link1.lastRxMs) > LINK_TIMEOUT_MS) {
    link1.lost = true;
    printLinkEvent("lost", now - link1.lastRxMs);
  }
}

static void queueRemoteCommand(uint8_t cmd) {
  ackState.cmdSeq++;
  ackState.cmd = cmd;
  ackState.target = REMOTE_NODE_ID;
  reloadAckPayload();
}

// ============================================================================
//  10) DURUM SATIRLARI ve SERİ KOMUTLAR
// ============================================================================
static void printHello() {
  char buf[180];
  snprintf(buf, sizeof(buf),
           "{\"type\":\"hello\",\"role\":\"gateway\",\"node\":%d,\"fw\":\"%s\",\"proto\":%d,\"radio\":%d,\"ch\":%d}",
           NODE_ID, FW_VERSION, PROTO_VERSION, radioOk ? 1 : 0, RF_CHANNEL);
  Serial.println(buf);
}

static void printStatus(uint32_t now) {
  const char* state = !link1.seen ? "never" : (link1.lost ? "lost" : "ok");
  const long age = link1.seen ? (long)(now - link1.lastRxMs) : -1L;
  char buf[220];
  snprintf(buf, sizeof(buf),
           "{\"type\":\"gw\",\"node\":%d,\"up\":%lu,\"radio\":%d,\"ch\":%d,\"rx\":%lu,\"lost\":%lu,"
           "\"lq\":%d,\"n1_link\":\"%s\",\"n1_age_ms\":%ld}",
           NODE_ID, (unsigned long)now, radioOk ? 1 : 0, RF_CHANNEL, (unsigned long)link1.rxCount,
           (unsigned long)link1.lostCount, linkQualityPct(), state, age);
  Serial.println(buf);
}

// Kullanıcı girdisini JSON'a güvenli yazmak için: [a-z0-9_-] dışındaki her karakter '?'
static void sanitizeToken(const char* in, char* out, size_t n) {
  size_t i = 0;
  for (; in[i] && i + 1 < n; i++) {
    const char c = in[i];
    const bool okc = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
    out[i] = okc ? c : '?';
  }
  out[i] = '\0';
}

static void printCmdResult(bool ok, const char* cmd, const char* target, const char* note) {
  char c[16];
  char t[16];
  sanitizeToken(cmd, c, sizeof(c));
  sanitizeToken(target, t, sizeof(t));
  char buf[220];
  snprintf(buf, sizeof(buf), "{\"type\":\"cmd\",\"ok\":%s,\"cmd\":\"%s\",\"target\":\"%s\",\"note\":\"%s\"}",
           ok ? "true" : "false", c, t, note);
  Serial.println(buf);
}

static void handleCommandLine(char* line, uint32_t now) {
  // küçük harfe çevir + baş/son boşlukları kırp
  for (char* c = line; *c; c++) *c = (char)tolower((unsigned char)*c);
  while (*line == ' ' || *line == '\t') line++;
  size_t len = strlen(line);
  while (len > 0 && (line[len - 1] == ' ' || line[len - 1] == '\t')) line[--len] = '\0';
  if (len == 0) return;

  char verb[16] = {0};
  char arg[16] = {0};
  sscanf(line, "%15s %15s", verb, arg);

  if (strcmp(verb, "ping") == 0) {
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"type\":\"pong\",\"node\":%d,\"up\":%lu}", NODE_ID, (unsigned long)now);
    Serial.println(buf);
  } else if (strcmp(verb, "status") == 0) {
    printHello();
    printStatus(now);
  } else if (strcmp(verb, "help") == 0) {
    Serial.println("{\"type\":\"help\",\"cmds\":\"silence 1|2|all, test 1|2, ping, status\"}");
  } else if (strcmp(verb, "silence") == 0) {
    const bool remote = strcmp(arg, "1") == 0 || strcmp(arg, "all") == 0;
    const bool local  = strcmp(arg, "2") == 0 || strcmp(arg, "all") == 0;
    if (!remote && !local) {
      printCmdResult(false, "silence", arg, "hedef 1, 2 veya all olmali");
      return;
    }
    if (local) {
      if (alarmBits != 0) silenced = true;
      printCmdResult(true, "silence", "2", "kopru buzzeri susturuldu");
    }
    if (remote) {
      if (!radioOk) {
        printCmdResult(false, "silence", "1", "radyo hazir degil");
      } else {
        queueRemoteCommand(CMD_SILENCE);
        printCmdResult(true, "silence", "1", "F1'in bir sonraki paketinin ACK'i ile iletilecek");
      }
    }
  } else if (strcmp(verb, "test") == 0) {
    if (strcmp(arg, "2") == 0) {
      buzzerChirp(3);
      printCmdResult(true, "test", "2", "kopru 3 bip");
    } else if (strcmp(arg, "1") == 0) {
      if (!radioOk) {
        printCmdResult(false, "test", "1", "radyo hazir degil");
      } else {
        queueRemoteCommand(CMD_TEST_BEEP);
        printCmdResult(true, "test", "1", "F1'in bir sonraki paketinin ACK'i ile iletilecek");
      }
    } else {
      printCmdResult(false, "test", arg, "hedef 1 veya 2 olmali");
    }
  } else {
    printCmdResult(false, verb, arg, "bilinmeyen komut (help yazin)");
  }
}

static void pollSerialCommands(uint32_t now) {
  while (Serial.available() > 0) {
    const char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (!cmdOverflow) {
        cmdBuf[cmdLen] = '\0';
        handleCommandLine(cmdBuf, now);
      }
      cmdLen = 0;
      cmdOverflow = false;
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    } else {
      cmdOverflow = true;                                    // çok uzun satır: tamamı yok sayılır
    }
  }
}

// ============================================================================
//  11) KURULUM ve ANA DÖNGÜ
// ============================================================================
void setup() {
  Serial.setRxBufferSize(512);
  Serial.begin(SERIAL_BAUD);

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
#if BUZZER_IS_ACTIVE
  digitalWrite(PIN_BUZZER, !BUZZER_ON_LEVEL);
#endif
  pinMode(PIN_BUZZER, OUTPUT);
  buzzerIsOn = true;
  buzzerWrite(false);
#if ENABLE_TEST_BUTTON
  pinMode(PIN_TEST_BTN, INPUT_PULLUP);
#endif
  analogReadResolution(12);

  dht.begin();

  radioOk = radioInit();
  const uint32_t now = millis();
  radioRetryAtMs = now + RADIO_RETRY_MS;
  radioHealthAtMs = now + RADIO_HEALTH_MS;

  printHello();
  if (!radioOk) logJson("error", "nRF24 bulunamadi: kablolari, 3V3 beslemesini ve kondansatoru kontrol edin");
  buzzerChirp(radioOk ? 1 : 3);

  sampleGas(now);
  lastMqMs = now;
  lastDhtMs = now - DHT_READ_MS + 1500UL;
  lastStatusMs = now;
  sendLocalTelemetry(PKT_BOOT, now);
}

void loop() {
  uint32_t now = millis();

  // 1) Radyo her şeyden önce: F1 paketi geldiyse anında seri porta
  pollRadio(now);
  checkLink(now);

  // 2) Radyo sağlığı / yeniden başlatma
  if (radioOk && (int32_t)(now - radioHealthAtMs) >= 0) {
    radioHealthAtMs = now + RADIO_HEALTH_MS;
    if (!radio.isChipConnected()) {
      radioOk = false;
      radioRetryAtMs = now;
      logJson("error", "nRF24 modulu yanit vermiyor, yeniden baslatilacak");
    }
  }
  if (!radioOk && (int32_t)(now - radioRetryAtMs) >= 0) {
    radioRetryAtMs = now + RADIO_RETRY_MS;
    radioOk = radioInit();
    if (radioOk) {
      logJson("info", "nRF24 yeniden baslatildi");
      buzzerChirp(1);
    }
  }

  // 3) Bilgisayardan gelen komutlar
  pollSerialCommands(now);

  // 4) Köprünün kendi sensörleri
  if (now - lastMqMs >= MQ_SAMPLE_MS) {
    lastMqMs = now;
    sampleGas(now);
  }
  if (now - lastDhtMs >= DHT_READ_MS) {
    lastDhtMs = now;
    readDht(now);
    pollRadio(millis());                                     // DHT okuması ~5 ms sürer; ardından radyoya tekrar bak
  }
  updateTestButton(now);

  const uint8_t prev = alarmBits;
  evaluateAlarms(now);
  if (alarmBits != prev) {
    if (alarmBits == 0 || prev == 0) silenced = false;
    buzzerUpdate(now);
    sendLocalTelemetry(PKT_EVENT, now);
  } else {
    const uint32_t period = alarmBits ? TX_PERIOD_ALARM_MS : TX_PERIOD_MS;
    if (now - lastTxMs >= period) sendLocalTelemetry(PKT_TELEMETRY, now);
  }

  // 5) Periyodik durum satırı
  now = millis();
  if (now - lastStatusMs >= STATUS_PERIOD_MS) {
    lastStatusMs = now;
    printStatus(now);
  }

  buzzerUpdate(now);
  ledUpdate(now);
  delay(1);
}
