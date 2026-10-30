/*
 * =============================================================================
 *  ORMAN YANGINI ERKEN UYARI MESH AĞI  ·  NODE 1 (F1)  ·  SAHA SENSÖR DÜĞÜMÜ
 * =============================================================================
 *  Kart      : ESP32-S (NodeMCU-32S / ESP-WROOM-32 tabanlı herhangi bir kart)
 *  Görev     : DHT11 (sıcaklık/nem) + MQ (gaz/duman) sensörünü okur, çakmakla
 *              oluşan gaz/ısı anomalisini anında algılar, buzzer'ı öttürür ve
 *              nRF24L01+PA+LNA ile Node 2'ye (F2 · köprü) paket gönderir.
 *  Güç       : 4×AA kalem pil → 5V/VIN pini   (ayrıntı: docs/HARDWARE.md)
 *  Protokol  : docs/PROTOCOL.md — paket yapısı Node 2 ile BİREBİR aynı olmalı
 *  Kütüphane : RF24 (TMRh20) ≥ 1.4.8 · DHT sensor library (Adafruit) ≥ 1.4.6
 *              · Adafruit Unified Sensor
 * -----------------------------------------------------------------------------
 *  BAĞLANTI (iki node'da da aynı; GPIO numarası esastır — kartta D4/IO4/P4/4)
 *    nRF24L01+PA+LNA : VCC→3V3 (+47µF elektrolitik ve 100nF, modül pinlerinde)
 *                      GND→GND  CE→GPIO4  CSN→GPIO5  SCK→GPIO18
 *                      MOSI→GPIO23  MISO→GPIO19  IRQ→boş
 *    DHT11           : VCC→3V3  GND→GND  DATA→GPIO27 (çıplak sensörse 10k pull-up)
 *    MQ gaz modülü   : VCC→5V/VIN  GND→GND  AO→[10k]→GPIO34→[10k]→GND  DO→boş
 *    Buzzer          : sinyal→GPIO25 (aktif buzzer modülü)
 *    Durum LED'i     : GPIO2 (kart üstü)      Test butonu: BOOT (GPIO0)
 * -----------------------------------------------------------------------------
 *  SESLİ TEŞHİS (bilgisayarsız sahada):
 *    açılışta 1 kısa bip  = her şey yolunda
 *    açılışta 3 kısa bip  = nRF24 modülü bulunamadı (kablo/besleme/kondansatör)
 *  LED: yavaş yanıp sönme = MQ ısınıyor · kısa flaş = paket teslim edildi
 *       çift flaş = radyo yok · hızlı yanıp sönme = ALARM
 * =============================================================================
 */
#include <Arduino.h>
#include <SPI.h>
#include <RF24.h>
#include <DHT.h>
#include <math.h>
#include <string.h>

// ============================================================================
//  1) AYARLAR — sahada değiştirilebilecek her şey burada
// ============================================================================
#define NODE_ID             1
#define FW_VERSION          "1.0.0"
#define SERIAL_BAUD         115200

// ---- Pinler (GPIO numarası) ----
#define PIN_NRF_CE          4
#define PIN_NRF_CSN         5
#define PIN_DHT             27
#define PIN_MQ_AO           34      // ADC1_CH6, sadece giriş pini
#define PIN_BUZZER          25
#define PIN_LED             2       // kart üstü mavi LED
#define PIN_TEST_BTN        0       // kart üstü BOOT butonu
#define PIN_VBAT            35      // opsiyonel pil ölçümü (ENABLE_VBAT 1 ise)

// ---- Buzzer ----
#define BUZZER_IS_ACTIVE    1       // 1: aktif buzzer (gerilim verince öter) · 0: pasif (PWM ton ister)
#define BUZZER_ON_LEVEL     HIGH    // modülün üstünde "low level trigger" yazıyorsa LOW yapın
#define BUZZER_TONE_HZ      2700    // yalnızca pasif buzzer için

// ---- MQ gaz sensörü ----
#define MQ_DIVIDER_RATIO    2.0f    // AO→10k→GPIO34→10k→GND  ⇒  AO = pin gerilimi × 2
#define MQ_WARMUP_MS        45000UL // ısınma: bu süre boyunca gaz alarmı verilmez
#define GAS_DELTA_MV        350.0f  // tabanın en az bu kadar mV üstü → GAZ
#define GAS_RATIO           1.40f   // ...ve tabanın en az 1.4 katı
#define GAS_CLEAR_HOLD_MS   4000UL  // eşiğin altında bu kadar kalınca GAZ alarmı söner

// ---- Sıcaklık (DHT11: 0–50 °C, ±2 °C, yavaş tepki) ----
#define TEMP_ALARM_C        45.0f   // mutlak eşik
#define TEMP_RISE_C         6.0f    // ortam tabanına göre ani artış eşiği
#define HEAT_CLEAR_HOLD_MS  6000UL
#define DHT_STALE_MS        10000UL // bu süredir geçerli okuma yoksa değer "geçersiz" gönderilir

// ---- Zamanlama ----
#define MQ_SAMPLE_MS        100UL
#define DHT_READ_MS         2100UL  // DHT11 en fazla ~1 Hz okunabilir; kütüphane 2 s önbellekler
#define TX_PERIOD_MS        1000UL  // normalde saniyede 1 paket
#define TX_PERIOD_ALARM_MS  300UL   // alarm sürerken daha sık paket
#define RADIO_RETRY_MS      5000UL  // radyo bulunamazsa yeniden deneme aralığı

// ---- Test butonu (BOOT) ----
#define ENABLE_TEST_BUTTON  1       // pil ile çalışan F1'de açık; USB'ye bağlı kartlarda 0 önerilir
#define TEST_BTN_HOLD_MS    800UL   // en az bu kadar basılı tutun
#define TEST_ALARM_MS       10000UL // test alarmı süresi

// ---- Radyo (iki node'da AYNI olmalı) ----
#define RF_CHANNEL          78      // 2478 MHz: ISM bandı içinde; Wi-Fi 1/6/11 ve BLE reklam kanalları dışında
#define RF_DATA_RATE        RF24_1MBPS
#define RF_PA_LEVEL         RF24_PA_LOW // masa üstü demo: LOW (yakında PA_MAX alıcıyı doyurur)
#define RF_SPI_HZ           4000000UL   // jumper kablolarla güvenli SPI hızı
#define RF_RETRY_DELAY      5       // (5+1)×250 µs = 1.5 ms (ACK payload için yeterli)
#define RF_RETRY_COUNT      15
static const uint8_t RF_ADDR[6] = "OYMA1"; // 5 baytlık boru adresi (diğer takımlarla çakışmasın)

// ---- Güç ----
#define CPU_MHZ             80      // pil ömrü için düşük frekans (80 / 160 / 240)
#define ENABLE_VBAT         0       // 1: GPIO35'ten pil gerilimini ölç (VIN→100k→GPIO35→47k→GND)
#define VBAT_DIVIDER_RATIO  3.128f  // (100k + 47k) / 47k

// ============================================================================
//  2) PROTOKOL — node2_gateway/src/main.cpp ile BİREBİR AYNI (docs/PROTOCOL.md)
// ============================================================================
#define PROTO_VERSION 1

enum : uint8_t { PKT_TELEMETRY = 0, PKT_EVENT = 1, PKT_BOOT = 2 };
enum : uint8_t { ALARM_GAS = 0x01, ALARM_HEAT = 0x02 };                 // bit maskesi; 3 = YANGIN (gaz + ısı)
enum : uint8_t { FLAG_WARMUP = 0x01, FLAG_DHT_ERR = 0x02, FLAG_SILENCED = 0x04, FLAG_TEST = 0x08 };
enum : uint8_t { CMD_NONE = 0, CMD_SILENCE = 1, CMD_TEST_BEEP = 2 };

#define TEMP_INVALID  ((int16_t)-32768)
#define HUM_INVALID   ((uint16_t)0xFFFF)

struct __attribute__((packed)) SensorPacket {
  uint8_t  proto;      // PROTO_VERSION
  uint8_t  nodeId;     // 1 = F1, 2 = F2
  uint8_t  type;       // PKT_*
  uint8_t  alarm;      // ALARM_* bit maskesi
  uint8_t  flags;      // FLAG_* bit maskesi
  uint8_t  txFail;     // son başarılı gönderimden beri başarısız deneme sayısı (255'te doyar)
  uint16_t seq;        // her pakette +1 (kayıp hesabı için)
  uint32_t uptimeMs;   // açılıştan beri geçen süre (ms)
  int16_t  tempX10;    // °C × 10   (geçersizse TEMP_INVALID)
  uint16_t humX10;     // %RH × 10  (geçersizse HUM_INVALID)
  uint16_t gasMv;      // MQ AO gerilimi, mV (bölücü telafi edilmiş)
  uint16_t gasBaseMv;  // MQ uyarlanabilir taban, mV
  uint16_t vbatMv;     // pil gerilimi, mV (0 = ölçülmüyor)
};
static_assert(sizeof(SensorPacket) == 22, "SensorPacket 22 bayt olmali");

struct __attribute__((packed)) AckPayload {
  uint8_t proto;       // PROTO_VERSION
  uint8_t cmdSeq;      // köprü her yeni komutta 1 artırır
  uint8_t cmd;         // CMD_*
  uint8_t target;      // hedef node (0 = hepsi)
};
static_assert(sizeof(AckPayload) == 4, "AckPayload 4 bayt olmali");

// ============================================================================
//  3) GLOBAL DURUM
// ============================================================================
static RF24 radio(PIN_NRF_CE, PIN_NRF_CSN, RF_SPI_HZ);
static DHT  dht(PIN_DHT, DHT11);

static bool     radioOk          = false;
static uint32_t radioRetryAtMs   = 0;
static uint16_t radioFailStreak  = 0;

static float    gasMv            = 0.0f;   // filtrelenmiş MQ gerilimi (mV)
static float    gasBaseMv        = 0.0f;   // uyarlanabilir temiz hava tabanı (mV)
static bool     gasInit          = false;

static float    tempC            = NAN;
static float    humPct           = NAN;
static float    tempBaseC        = NAN;
static bool     dhtAttempted     = false;
static bool     dhtEverOk        = false;
static uint32_t lastDhtOkMs      = 0;

static uint8_t  realAlarm        = 0;      // sensörlerden gelen alarm bitleri
static uint8_t  alarmBits        = 0;      // gönderilen alarm (gerçek | test)
static bool     gasClearing      = false;
static uint32_t gasClearSinceMs  = 0;
static bool     heatClearing     = false;
static uint32_t heatClearSinceMs = 0;
static bool     testActive       = false;
static uint32_t testUntilMs      = 0;
static bool     silenced         = false;

static bool     btnDown          = false;
static bool     btnFired         = false;
static uint32_t btnDownSinceMs   = 0;

static bool     cmdSeqKnown      = false;
static uint8_t  lastCmdSeq       = 0;

static uint16_t txSeq            = 0;
static uint8_t  txFail           = 0;
static uint32_t lastTxMs         = 0;
static uint32_t lastTxOkMs       = 0;
static uint32_t lastMqMs         = 0;
static uint32_t lastDhtMs        = 0;

// ============================================================================
//  4) YARDIMCILAR
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

// x10 tamsayıyı "12.3" biçimine çevirir (float printf'e güvenmeden)
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

// Seri porta tek satır JSON yazar (docs/PROTOCOL.md → "tel" mesajı)
//   via   : "usb" (node kendi portundan) | "rf" (köprü radyodan aldı) | "local" (köprünün kendi sensörü)
//   lq    : bağlantı kalitesi % (yoksa -1)      rfOk : radyo teslimi 1/0 (yoksa -1)
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

// ============================================================================
//  5) BUZZER ve LED (bloklamayan)
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
    buzzerWrite((now % 300UL) < 180UL);   // siren: 180 ms açık / 120 ms kapalı
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
    on = (now % 200UL) < 100UL;                              // ALARM: hızlı
  } else if (!radioOk) {
    const uint32_t ph = now % 1000UL;
    on = ph < 80UL || (ph >= 200UL && ph < 280UL);           // radyo yok: çift flaş
  } else if (isWarmingUp(now)) {
    on = (now % 2000UL) < 1000UL;                            // MQ ısınıyor: yavaş
  } else {
    on = (now - lastTxOkMs) < 40UL;                          // paket teslim edildi: kısa flaş
  }
  digitalWrite(PIN_LED, on ? HIGH : LOW);
}

// ============================================================================
//  6) SENSÖRLER
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
  gasMv = gasMv * 0.5f + mv * 0.5f;                          // hafif gürültü filtresi, tepki hızlı kalır
  if (isWarmingUp(now)) {
    gasBaseMv += (gasMv - gasBaseMv) * 0.05f;                // ısınırken tabanı hızlı izle (~2 s)
  } else if (!(realAlarm & ALARM_GAS) && gasMv < gasBaseMv + GAS_DELTA_MV * 0.5f) {
    gasBaseMv += (gasMv - gasBaseMv) * 0.002f;               // normalde yavaş sürüklenme takibi (~50 s)
  }
}

static void readDht(uint32_t now) {
  dhtAttempted = true;
  const float t = dht.readTemperature();
  const float h = dht.readHumidity();
  if (isnan(t) || isnan(h)) return;                          // son geçerli değer DHT_STALE_MS boyunca korunur
  tempC = t;
  humPct = h;
  dhtEverOk = true;
  lastDhtOkMs = now;
  if (isnan(tempBaseC)) {
    tempBaseC = t;
  } else if (!(realAlarm & ALARM_HEAT) && (t - tempBaseC) < TEMP_RISE_C * 0.5f) {
    tempBaseC += (t - tempBaseC) * 0.05f;                    // ortam sıcaklığını yavaş izle (~40 s)
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
//  7) ALARM MANTIĞI
// ============================================================================
static void evaluateAlarms(uint32_t now) {
  uint8_t next = realAlarm;

  // --- GAZ: ısınma bitmiş ve (taban + Δ) ile (taban × oran) eşiklerinin büyüğü aşılmışsa ---
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

  // --- ISI: mutlak eşik ya da ortam tabanına göre ani artış ---
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
    next = (uint8_t)(next & ~ALARM_HEAT);                    // sensör yoksa ısı alarmı tutulmaz
    heatClearing = false;
  }
  realAlarm = next;

  // --- TEST: BOOT butonu → GAZ + ISI (FLAG_TEST ile işaretlenir) ---
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
//  8) RADYO
// ============================================================================
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
  radio.openWritingPipe(RF_ADDR);   // RF24 ≥1.6'da stopListening(RF_ADDR) da olur; bu biçim tüm sürümlerde çalışır
  radio.stopListening();
  return true;
}

static void executeCommand(uint8_t cmd) {
  switch (cmd) {
    case CMD_SILENCE:
      if (alarmBits != 0) silenced = true;
      logJson("info", "merkezden SUSTUR komutu alindi");
      break;
    case CMD_TEST_BEEP:
      buzzerChirp(3);
      logJson("info", "merkezden TEST BIP komutu alindi");
      break;
    default:
      break;
  }
}

// Köprü, her ACK paketinin içine güncel komut durumunu koyar (docs/PROTOCOL.md)
static void handleAckPayloads() {
  while (radio.available()) {
    const uint8_t len = radio.getDynamicPayloadSize();
    if (len == 0) break;                                     // bozuk boyut: kütüphane RX FIFO'yu boşalttı
    uint8_t buf[32];
    radio.read(buf, len);
    if (len < sizeof(AckPayload)) continue;
    AckPayload a;
    memcpy(&a, buf, sizeof(a));
    if (a.proto != PROTO_VERSION) continue;
    if (!cmdSeqKnown) {                                      // açılıştan sonraki ilk ACK: eski komutu çalıştırma
      cmdSeqKnown = true;
      lastCmdSeq = a.cmdSeq;
      continue;
    }
    if (a.cmdSeq == lastCmdSeq) continue;
    lastCmdSeq = a.cmdSeq;
    if (a.target == NODE_ID || a.target == 0) executeCommand(a.cmd);
  }
}

static void sendPacket(uint8_t type, uint32_t now) {
  SensorPacket p;
  memset(&p, 0, sizeof(p));
  p.proto     = PROTO_VERSION;
  p.nodeId    = NODE_ID;
  p.type      = type;
  p.alarm     = alarmBits;
  p.flags     = currentFlags(now);
  p.txFail    = txFail;
  p.seq       = txSeq++;
  p.uptimeMs  = now;
  p.tempX10   = dhtFresh(now) ? (int16_t)lroundf(tempC * 10.0f) : TEMP_INVALID;
  p.humX10    = dhtFresh(now) ? (uint16_t)lroundf(humPct * 10.0f) : HUM_INVALID;
  p.gasMv     = (uint16_t)constrain(lroundf(gasMv), 0L, 65534L);
  p.gasBaseMv = (uint16_t)constrain(lroundf(gasBaseMv), 0L, 65534L);
  p.vbatMv    = readVbatMv();

  int rfOk = 0;
  if (radioOk) {
    const uint8_t attempts = (type == PKT_TELEMETRY) ? 1 : 3;   // olay/açılış paketinde ek deneme
    bool ok = false;
    for (uint8_t i = 0; i < attempts && !ok; i++) ok = radio.write(&p, sizeof(p));
    if (ok) {
      rfOk = 1;
      txFail = 0;
      radioFailStreak = 0;
      lastTxOkMs = now;
      handleAckPayloads();
    } else {
      if (txFail < 255) txFail++;
      if (++radioFailStreak >= 10 && !radio.isChipConnected()) {   // modül koptu / beslemesi çöktü
        radioOk = false;
        radioRetryAtMs = now + 2000UL;
        logJson("error", "nRF24 modulu yanit vermiyor, yeniden baslatilacak");
      }
    }
  } else if (txFail < 255) {
    txFail++;
  }
  lastTxMs = now;
  printPacketJson(p, "usb", -1, rfOk);   // USB'ye takılıysa simülasyon doğrudan da okuyabilir (B planı)
}

// ============================================================================
//  9) KURULUM ve ANA DÖNGÜ
// ============================================================================
void setup() {
#if CPU_MHZ != 240
  setCpuFrequencyMhz(CPU_MHZ);
#endif
  Serial.begin(SERIAL_BAUD);

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
#if BUZZER_IS_ACTIVE
  digitalWrite(PIN_BUZZER, !BUZZER_ON_LEVEL);
#endif
  pinMode(PIN_BUZZER, OUTPUT);
  buzzerIsOn = true;                 // ilk yazımı zorla
  buzzerWrite(false);
#if ENABLE_TEST_BUTTON
  pinMode(PIN_TEST_BTN, INPUT_PULLUP);
#endif
  analogReadResolution(12);          // GPIO34: varsayılan 11 dB zayıflatma (~0–3.1 V)

  dht.begin();

  radioOk = radioInit();
  radioRetryAtMs = millis() + RADIO_RETRY_MS;

  char hello[160];
  snprintf(hello, sizeof(hello),
           "{\"type\":\"hello\",\"role\":\"sensor\",\"node\":%d,\"fw\":\"%s\",\"proto\":%d,\"radio\":%d,\"ch\":%d}",
           NODE_ID, FW_VERSION, PROTO_VERSION, radioOk ? 1 : 0, RF_CHANNEL);
  Serial.println(hello);
  if (!radioOk) logJson("error", "nRF24 bulunamadi: kablolari, 3V3 beslemesini ve kondansatoru kontrol edin");

  buzzerChirp(radioOk ? 1 : 3);      // sesli teşhis: 1 bip tamam, 3 bip radyo yok

  const uint32_t now = millis();
  sampleGas(now);
  lastMqMs = now;
  lastDhtMs = now - DHT_READ_MS + 1500UL;   // ilk DHT okuması ~1.5 s sonra (sensör açılış süresi)
  sendPacket(PKT_BOOT, now);
}

void loop() {
  const uint32_t now = millis();

  if (!radioOk && (int32_t)(now - radioRetryAtMs) >= 0) {
    radioOk = radioInit();
    radioRetryAtMs = now + RADIO_RETRY_MS;
    if (radioOk) {
      radioFailStreak = 0;
      logJson("info", "nRF24 yeniden baslatildi");
      buzzerChirp(1);
    }
  }

  if (now - lastMqMs >= MQ_SAMPLE_MS) {
    lastMqMs = now;
    sampleGas(now);
  }
  if (now - lastDhtMs >= DHT_READ_MS) {
    lastDhtMs = now;
    readDht(now);
  }
  updateTestButton(now);

  const uint8_t prev = alarmBits;
  evaluateAlarms(now);
  if (alarmBits != prev) {
    if (alarmBits == 0 || prev == 0) silenced = false;   // yeni alarm ya da alarm bitti → susturma sıfırlanır
    buzzerUpdate(now);                                   // buzzer'ı radyodan ÖNCE başlat
    sendPacket(PKT_EVENT, now);                          // değişimi beklemeden bildir
  } else {
    const uint32_t period = alarmBits ? TX_PERIOD_ALARM_MS : TX_PERIOD_MS;
    if (now - lastTxMs >= period) sendPacket(PKT_TELEMETRY, now);
  }

  buzzerUpdate(millis());
  ledUpdate(millis());
  delay(1);
}
