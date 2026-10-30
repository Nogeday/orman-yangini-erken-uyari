#pragma once
#include <cstdint>
#include <cstring>
#include <deque>
#include <vector>
typedef enum { RF24_PA_MIN = 0, RF24_PA_LOW, RF24_PA_HIGH, RF24_PA_MAX, RF24_PA_ERROR } rf24_pa_dbm_e;
typedef enum { RF24_1MBPS = 0, RF24_2MBPS, RF24_250KBPS } rf24_datarate_e;
typedef enum { RF24_CRC_DISABLED = 0, RF24_CRC_8, RF24_CRC_16 } rf24_crclength_e;
uint32_t millis();
class RF24 {
 public:
  bool present = true, link = true, listening = false;
  std::deque<std::vector<uint8_t>> rxq;          // gelen (node2: F1 paketleri, node1: ACK payload)
  std::vector<std::pair<uint32_t, std::vector<uint8_t>>> txlog; // node1: teslim edilen/edilmeyen
  std::vector<bool> txok;
  std::vector<uint8_t> ackState;                 // node1 sürücüsü: köprünün ACK durumu
  std::vector<uint8_t> lastAckWritten; int ackWrites = 0, flushes = 0;
  RF24(uint8_t, uint8_t, uint32_t = 10000000) {}
  bool begin() { return present; }
  bool isChipConnected() { return present; }
  void setChannel(uint8_t) {}
  bool setDataRate(rf24_datarate_e) { return true; }
  void setPALevel(uint8_t, bool = true) {}
  void setCRCLength(rf24_crclength_e) {}
  void setAutoAck(bool) {}
  void setRetries(uint8_t, uint8_t) {}
  void enableDynamicPayloads() {}
  void enableAckPayload() {}
  void openWritingPipe(const uint8_t*) {}
  void openReadingPipe(uint8_t, const uint8_t*) {}
  void startListening() { listening = true; }
  void stopListening() { listening = false; }
  bool write(const void* buf, uint8_t len) {
    std::vector<uint8_t> v((const uint8_t*)buf, (const uint8_t*)buf + len);
    bool ok = present && link;
    txlog.push_back({millis(), v}); txok.push_back(ok);
    if (ok && !ackState.empty()) rxq.push_back(ackState);
    return ok;
  }
  bool available() { return !rxq.empty(); }
  bool available(uint8_t* pipe) { if (pipe) *pipe = 1; return !rxq.empty(); }
  uint8_t getDynamicPayloadSize() { if (rxq.empty()) return 0; size_t n = rxq.front().size(); if (n > 32) { rxq.clear(); return 0; } return (uint8_t)n; }
  void read(void* buf, uint8_t len) { if (rxq.empty()) return; auto v = rxq.front(); rxq.pop_front(); memcpy(buf, v.data(), len < v.size() ? len : v.size()); }
  void flush_tx() { flushes++; }
  void flush_rx() { rxq.clear(); }
  bool writeAckPayload(uint8_t, const void* buf, uint8_t len) { lastAckWritten.assign((const uint8_t*)buf, (const uint8_t*)buf + len); ackWrites++; return true; }
};
