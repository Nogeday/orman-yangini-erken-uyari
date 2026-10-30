#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <math.h>
#include <string>
#include <vector>
#include <deque>
#define HIGH 0x1
#define LOW  0x0
#define INPUT 0x01
#define OUTPUT 0x03
#define INPUT_PULLUP 0x05
#define F(x) (x)
#define constrain(amt,low,high) ((amt)<(low)?(low):((amt)>(high)?(high):(amt)))
using std::isnan;
uint32_t millis();
void delay(uint32_t ms);
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t val);
int digitalRead(uint8_t pin);
uint32_t analogReadMilliVolts(uint8_t pin);
void analogReadResolution(uint8_t bits);
bool setCpuFrequencyMhz(uint32_t mhz);
void tone(uint8_t pin, unsigned int freq, unsigned long duration = 0);
void noTone(uint8_t pin);
struct SerialMock {
  std::deque<char> in;
  std::vector<std::pair<uint32_t, std::string>> out;
  void begin(unsigned long) {}
  size_t setRxBufferSize(size_t n) { return n; }
  void println(const char* s);
  int available() { return (int)in.size(); }
  int read() { if (in.empty()) return -1; char c = in.front(); in.pop_front(); return (unsigned char)c; }
  void feed(const std::string& s) { for (char c : s) in.push_back(c); }
};
extern SerialMock Serial;
