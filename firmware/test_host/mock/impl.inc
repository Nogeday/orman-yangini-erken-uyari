// ---- mock tanımları ----
SerialMock Serial;
static uint32_t g_now = 0;
float g_temp = 24.0f, g_hum = 45.0f; bool g_dht_fail = false;
static float g_mq_pin_mv = 150.0f;
static int g_btn = HIGH;
static uint8_t g_pins[40] = {0};
static std::vector<std::pair<uint32_t,int>> g_buzzer_edges;
static bool g_verbose = false;
uint32_t millis() { return g_now; }
void delay(uint32_t ms) { g_now += ms; }
void pinMode(uint8_t, uint8_t) {}
void digitalWrite(uint8_t pin, uint8_t val) {
  if (pin == 25 && g_pins[pin] != val) g_buzzer_edges.push_back({g_now, val});
  g_pins[pin] = val;
}
int digitalRead(uint8_t pin) { return pin == 0 ? g_btn : g_pins[pin]; }
static uint32_t g_noise = 12345;
uint32_t analogReadMilliVolts(uint8_t) {
  g_noise = g_noise * 1103515245u + 12345u;
  int n = (int)((g_noise >> 16) % 11) - 5;
  float v = g_mq_pin_mv + n; if (v < 142) v = 142;   // ESP32 ADC alt sınırı benzetimi
  if (v > 3100) v = 3100;
  return (uint32_t)v;
}
void analogReadResolution(uint8_t) {}
bool setCpuFrequencyMhz(uint32_t) { return true; }
void tone(uint8_t, unsigned int, unsigned long) {}
void noTone(uint8_t) {}
void SerialMock::println(const char* s) { out.push_back({g_now, s}); if (g_verbose) printf("[%7.3f] %s\n", g_now / 1000.0, s); }
