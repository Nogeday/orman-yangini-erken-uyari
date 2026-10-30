#pragma once
#include <cstdint>
#include <cmath>
#define DHT11 11
extern float g_temp, g_hum; extern bool g_dht_fail;
class DHT {
 public:
  DHT(uint8_t, uint8_t) {}
  void begin() {}
  float readTemperature() { return g_dht_fail ? NAN : std::round(g_temp); }
  float readHumidity() { return g_dht_fail ? NAN : std::round(g_hum); }
};
