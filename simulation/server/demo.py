"""Demo ve replay kaynakları.

Sanal F1 ve F2, firmware'in (firmware/node1_sensor, firmware/node2_gateway → main.cpp)
örnekleme, alarm, paket ve ACK-komut mantığının Python eşidir. Ürettikleri satırlar
firmware ile BİREBİR aynı biçimdedir (protocol.format_*) ve gerçek seri satırlarla aynı
ayrıştırıcıdan geçer. Değişken adları firmware'dekilerle eşleşir.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Callable, Optional

from .protocol import (
    ALARM_GAS, ALARM_HEAT, FLAG_DHT_ERR, FLAG_SILENCED, FLAG_WARMUP, HUM_INVALID, TEMP_INVALID,
    format_cmd, format_gw, format_hello, format_link, format_tel,
)

# ---- firmware sabitleri (main.cpp §1) ----
MQ_WARMUP_MS = 45_000
GAS_DELTA_MV = 350.0
GAS_RATIO = 1.40
GAS_CLEAR_HOLD_MS = 4_000
TEMP_ALARM_C = 45.0
TEMP_RISE_C = 6.0
HEAT_CLEAR_HOLD_MS = 6_000
DHT_STALE_MS = 10_000
MQ_SAMPLE_MS = 100
DHT_READ_MS = 2_100
TX_PERIOD_MS = 1_000
TX_PERIOD_ALARM_MS = 300
LINK_TIMEOUT_MS = 3_500
STATUS_PERIOD_MS = 5_000
PKT_TELEMETRY, PKT_EVENT, PKT_BOOT = 0, 1, 2
CMD_NONE, CMD_SILENCE, CMD_TEST_BEEP = 0, 1, 2
STEP_MS = 10

# ---- "Simüle Çakmak Tetikle" senaryosu (saniye, tetikten itibaren) ----
LIGHTER_GAS_MV = 800.0      # gaz 300 ms içinde taban + 800 mV'a çıkar
LIGHTER_RISE_S = 0.3
HEAT_START_S = 6.0          # 6 s sonra sıcaklık yükselir → GAZ + ISI = YANGIN
HEAT_RISE_C = 12.0
STIMULUS_END_S = 25.0       # 25 s sonra kaynak kalkar; alarmlar firmware histerezisiyle söner
SCENARIO_END_S = 45.0


class Environment:
    """Sanal sensörün gördüğü fiziksel ortam (MQ AO gerilimi, sıcaklık, nem)."""

    def __init__(self, rng: random.Random, gas_mv: float, temp_c: float, hum_pct: float,
                 warm_extra_mv: float = 0.0, warm_tau_s: float = 14.0) -> None:
        self.rng = rng
        self.gas0, self.temp0, self.hum0 = gas_mv, temp_c, hum_pct
        self.warm_extra_mv, self.warm_tau_s = warm_extra_mv, warm_tau_s
        self.lighter_at_s: Optional[float] = None

    def lighter(self, t_s: float) -> None:
        self.lighter_at_s = t_s

    def clear(self) -> None:
        self.lighter_at_s = None

    def scenario_age(self, t_s: float) -> Optional[float]:
        return None if self.lighter_at_s is None else t_s - self.lighter_at_s

    def _gas_add(self, tau: float) -> float:
        if tau < 0:
            return 0.0
        if tau < STIMULUS_END_S:
            return LIGHTER_GAS_MV * min(1.0, tau / LIGHTER_RISE_S)
        return LIGHTER_GAS_MV * math.exp(-(tau - STIMULUS_END_S) / 1.5)

    def _heat_add(self, tau: float) -> float:
        if tau < HEAT_START_S:
            return 0.0
        rise = HEAT_RISE_C * (1.0 - math.exp(-(min(tau, STIMULUS_END_S) - HEAT_START_S) / 2.5))
        if tau < STIMULUS_END_S:
            return rise
        return rise * math.exp(-(tau - STIMULUS_END_S) / 4.0)

    def gas_mv(self, t_s: float, up_s: float) -> float:
        v = self.gas0 + self.warm_extra_mv * math.exp(-up_s / self.warm_tau_s) + self.rng.gauss(0.0, 3.0)
        tau = self.scenario_age(t_s)
        if tau is not None:
            v += self._gas_add(tau)
        return max(0.0, v)

    def temp_c(self, t_s: float) -> float:
        tau = self.scenario_age(t_s)
        return self.temp0 + (self._heat_add(tau) if tau is not None else 0.0)

    def hum_pct(self, t_s: float) -> float:
        tau = self.scenario_age(t_s)
        return self.hum0 - (0.8 * self._heat_add(tau) if tau is not None else 0.0) + self.rng.choice((0.0, 0.0, 1.0))


class VirtualSensor:
    """node1_sensor (ve köprünün kendi sensörü) main.cpp mantığı: sampleGas, readDht,
    evaluateAlarms, currentFlags ve paket zamanlaması."""

    def __init__(self, node_id: int, env: Environment, boot_ms: int = 0, warmup: bool = True) -> None:
        self.node_id = node_id
        self.env = env
        self.millis = boot_ms
        self.warmup_until = MQ_WARMUP_MS if warmup else 0
        self.gasMv = 0.0
        self.gasBaseMv = 0.0
        self.gasInit = False
        self.tempC = math.nan
        self.humPct = math.nan
        self.tempBaseC = math.nan
        self.dhtAttempted = False
        self.dhtEverOk = False
        self.lastDhtOkMs = 0
        self.realAlarm = 0
        self.alarmBits = 0
        self.gasClearing = False
        self.gasClearSinceMs = 0
        self.heatClearing = False
        self.heatClearSinceMs = 0
        self.silenced = False
        self.cmdSeqKnown = False
        self.lastCmdSeq = 0
        self.txSeq = 0
        self.lastTxMs = 0
        self.lastMqMs = 0
        self.lastDhtMs = 0
        self.booted = False

    # -- yardımcılar --
    def is_warming(self, now: int) -> bool:
        return now < self.warmup_until

    def skip_warmup(self) -> None:
        self.warmup_until = 0

    def dht_fresh(self, now: int) -> bool:
        return self.dhtEverOk and (now - self.lastDhtOkMs) < DHT_STALE_MS

    def _t_s(self, now: int) -> float:
        return now / 1000.0

    # -- sensörler --
    def sample_gas(self, now: int) -> None:
        mv = self.env.gas_mv(self._t_s(now), now / 1000.0)
        if not self.gasInit:
            self.gasMv = self.gasBaseMv = mv
            self.gasInit = True
            return
        self.gasMv = self.gasMv * 0.5 + mv * 0.5
        if self.is_warming(now):
            self.gasBaseMv += (self.gasMv - self.gasBaseMv) * 0.05
        elif not (self.realAlarm & ALARM_GAS) and self.gasMv < self.gasBaseMv + GAS_DELTA_MV * 0.5:
            self.gasBaseMv += (self.gasMv - self.gasBaseMv) * 0.002

    def read_dht(self, now: int) -> None:
        self.dhtAttempted = True
        t = float(round(self.env.temp_c(self._t_s(now))))     # DHT11: 1 °C, %1 çözünürlük
        h = float(round(self.env.hum_pct(self._t_s(now))))
        self.tempC, self.humPct = t, h
        self.dhtEverOk = True
        self.lastDhtOkMs = now
        if math.isnan(self.tempBaseC):
            self.tempBaseC = t
        elif not (self.realAlarm & ALARM_HEAT) and (t - self.tempBaseC) < TEMP_RISE_C * 0.5:
            self.tempBaseC += (t - self.tempBaseC) * 0.05

    # -- alarm mantığı --
    def evaluate_alarms(self, now: int) -> None:
        nxt = self.realAlarm
        gas_thr = max(self.gasBaseMv + GAS_DELTA_MV, self.gasBaseMv * GAS_RATIO)
        if self.gasInit and not self.is_warming(now) and self.gasMv >= gas_thr:
            nxt |= ALARM_GAS
            self.gasClearing = False
        elif nxt & ALARM_GAS:
            if self.gasMv < self.gasBaseMv + GAS_DELTA_MV * 0.5:
                if not self.gasClearing:
                    self.gasClearing, self.gasClearSinceMs = True, now
                elif now - self.gasClearSinceMs >= GAS_CLEAR_HOLD_MS:
                    nxt &= ~ALARM_GAS
                    self.gasClearing = False
            else:
                self.gasClearing = False
        if self.dht_fresh(now) and not math.isnan(self.tempBaseC):
            rise = self.tempC - self.tempBaseC
            if self.tempC >= TEMP_ALARM_C or rise >= TEMP_RISE_C:
                nxt |= ALARM_HEAT
                self.heatClearing = False
            elif nxt & ALARM_HEAT:
                if self.tempC < TEMP_ALARM_C - 2.0 and rise < TEMP_RISE_C - 2.0:
                    if not self.heatClearing:
                        self.heatClearing, self.heatClearSinceMs = True, now
                    elif now - self.heatClearSinceMs >= HEAT_CLEAR_HOLD_MS:
                        nxt &= ~ALARM_HEAT
                        self.heatClearing = False
                else:
                    self.heatClearing = False
        elif not self.dht_fresh(now):
            nxt &= ~ALARM_HEAT
            self.heatClearing = False
        self.realAlarm = nxt
        self.alarmBits = nxt

    def current_flags(self, now: int) -> int:
        f = 0
        if self.is_warming(now):
            f |= FLAG_WARMUP
        if self.dhtAttempted and not self.dht_fresh(now):
            f |= FLAG_DHT_ERR
        if self.silenced:
            f |= FLAG_SILENCED
        return f

    def clear_alarm_state(self) -> None:
        """Sıfırla: sanal düğümü yeniden başlatmış gibi alarm durum makinesini temizler."""
        self.realAlarm = 0
        self.gasClearing = self.heatClearing = False
        self.silenced = False
        self.gasMv = self.gasBaseMv
        if not math.isnan(self.tempBaseC):
            self.tempC = self.tempBaseC

    # -- paketler --
    def _packet(self, ptype: int, now: int) -> dict:
        fresh = self.dht_fresh(now)
        p = {
            "node": self.node_id, "ptype": ptype, "alarm": self.alarmBits, "flags": self.current_flags(now),
            "seq": self.txSeq, "up": now,
            "temp_x10": int(round(self.tempC * 10)) if fresh else TEMP_INVALID,
            "hum_x10": int(round(self.humPct * 10)) if fresh else HUM_INVALID,
            "gas": int(min(65534, max(0, round(self.gasMv)))),
            "base": int(min(65534, max(0, round(self.gasBaseMv)))),
        }
        self.txSeq = (self.txSeq + 1) & 0xFFFF
        self.lastTxMs = now
        return p

    def boot(self, announce: bool = True) -> dict:
        """setup(). announce=False: zaten çalışan bir saha düğümünü sürdürür (açılış paketi yok,
        DHT hemen okunur) — donanım sürerken başlatılan simülasyon için."""
        now = self.millis
        self.sample_gas(now)
        self.lastMqMs = now
        self.booted = True
        if announce:
            self.lastDhtMs = now - DHT_READ_MS + 1500
            return self._packet(PKT_BOOT, now)
        self.lastDhtMs = now
        self.read_dht(now)
        return self._packet(PKT_TELEMETRY, now)

    def step(self, now: int) -> Optional[dict]:
        """Bir loop() turu. Gönderilecek paket varsa döner."""
        self.millis = now
        if now - self.lastMqMs >= MQ_SAMPLE_MS:
            self.lastMqMs = now
            self.sample_gas(now)
        if now - self.lastDhtMs >= DHT_READ_MS:
            self.lastDhtMs = now
            self.read_dht(now)
        prev = self.alarmBits
        self.evaluate_alarms(now)
        if self.alarmBits != prev:
            if self.alarmBits == 0 or prev == 0:
                self.silenced = False
            return self._packet(PKT_EVENT, now)
        period = TX_PERIOD_ALARM_MS if self.alarmBits else TX_PERIOD_MS
        if now - self.lastTxMs >= period:
            return self._packet(PKT_TELEMETRY, now)
        return None

    def handle_ack(self, ack: tuple[int, int, int]) -> None:
        """handleAckPayloads(): açılıştan sonraki ilk ACK'teki komut çalıştırılmaz."""
        cmd_seq, cmd, target = ack
        if not self.cmdSeqKnown:
            self.cmdSeqKnown, self.lastCmdSeq = True, cmd_seq
            return
        if cmd_seq == self.lastCmdSeq:
            return
        self.lastCmdSeq = cmd_seq
        if target in (self.node_id, 0) and cmd == CMD_SILENCE and self.alarmBits != 0:
            self.silenced = True


class VirtualBridge:
    """node2_gateway main.cpp: F1 paketini anında `via:"rf"` satırı olarak basar, kayan
    pencereyle bağlantı kalitesi (lq) hesaplar, 3.5 s paket gelmezse `link lost` üretir,
    komutları ACK payload ile F1'e geri iletir."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit
        self.seen = False
        self.lost = False
        self.lastSeq = 0
        self.lastUptimeMs = 0
        self.lastRxMs = 0
        self.rxCount = 0
        self.lostCount = 0
        self.winRx = 0.0
        self.winExp = 0.0
        self.ack = (0, CMD_NONE, 1)      # (cmdSeq, cmd, target)

    def lq(self) -> int:
        if self.winExp <= 0:
            return -1
        return min(100, int(round(100.0 * self.winRx / self.winExp)))

    def on_radio(self, p: dict, now: int) -> tuple[int, int, int]:
        if self.seen and p["seq"] == self.lastSeq and p["up"] == self.lastUptimeMs:
            return self.ack
        restarted = not self.seen or p["ptype"] == PKT_BOOT or p["up"] < self.lastUptimeMs
        if restarted:
            self.winRx, self.winExp = 0.0, 1.0
        else:
            gap = (p["seq"] - self.lastSeq) & 0xFFFF
            if gap > 1000:
                self.winRx, self.winExp = 0.0, 1.0
            else:
                self.lostCount += gap - 1
                self.winExp += gap
        self.winRx += 1.0
        if self.winExp > 60.0:
            self.winRx *= 0.5
            self.winExp *= 0.5
        self.winRx = min(self.winRx, self.winExp)
        if self.lost:
            self.lost = False
            self.emit(format_link(1, "ok", now - self.lastRxMs))
        self.seen = True
        self.lastSeq, self.lastUptimeMs, self.lastRxMs = p["seq"], p["up"], now
        self.rxCount += 1
        self.emit(format_tel(node=p["node"], via="rf", ptype=p["ptype"], seq=p["seq"], up=p["up"],
                             temp_x10=p["temp_x10"], hum_x10=p["hum_x10"], gas=p["gas"], base=p["base"],
                             alarm=p["alarm"], flags=p["flags"], lq=self.lq()))
        return self.ack

    def check_link(self, now: int) -> None:
        if self.seen and not self.lost and now - self.lastRxMs > LINK_TIMEOUT_MS:
            self.lost = True
            self.emit(format_link(1, "lost", now - self.lastRxMs))

    def command(self, line: str, local_sensor: Optional[VirtualSensor]) -> None:
        parts = line.strip().lower().split()
        verb = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if verb == "silence":
            remote = arg in ("1", "all")
            local = arg in ("2", "all")
            if not remote and not local:
                self.emit(format_cmd(False, "silence", arg, "hedef 1, 2 veya all olmali"))
                return
            if local:
                if local_sensor is not None and local_sensor.alarmBits != 0:
                    local_sensor.silenced = True
                self.emit(format_cmd(True, "silence", "2", "kopru buzzeri susturuldu"))
            if remote:
                self.ack = ((self.ack[0] + 1) & 0xFF, CMD_SILENCE, 1)
                self.emit(format_cmd(True, "silence", "1", "F1'in bir sonraki paketinin ACK'i ile iletilecek"))
        elif verb == "test":
            if arg == "2":
                self.emit(format_cmd(True, "test", "2", "kopru 3 bip"))
            elif arg == "1":
                self.ack = ((self.ack[0] + 1) & 0xFF, CMD_TEST_BEEP, 1)
                self.emit(format_cmd(True, "test", "1", "F1'in bir sonraki paketinin ACK'i ile iletilecek"))
            else:
                self.emit(format_cmd(False, "test", arg, "hedef 1 veya 2 olmali"))
        else:
            self.emit(format_cmd(False, verb, arg, "bilinmeyen komut (help yazin)"))


class DemoNetwork:
    """Sanal F1 (+ isteğe bağlı sanal F2 köprüsü). `tick(now_s)` ile ilerler; satırları
    `emit_line` ile verir.

    with_f2=False: donanım ya da replay sürerken "Simüle Çakmak Tetikle" için yalnızca sanal F1
    (ısınmış) çalışır; gerçek F2'nin satırlarıyla karışmasın diye köprü kendi satırlarını basmaz.
    """

    def __init__(self, emit_line: Callable[[str], None], *, seed: int = 7, with_f2: bool = True,
                 f1_boot_ms: int = 0, f1_warmup: bool = True) -> None:
        self.emit_line = emit_line
        self.rng = random.Random(seed)
        self.with_f2 = with_f2
        self.env1 = Environment(self.rng, gas_mv=312.0, temp_c=24.0, hum_pct=45.0,
                                warm_extra_mv=490.0 if f1_warmup else 0.0)
        self.f1 = VirtualSensor(1, self.env1, boot_ms=f1_boot_ms, warmup=f1_warmup)
        self.bridge = VirtualBridge(emit_line)
        self.f2: Optional[VirtualSensor] = None
        if with_f2:
            self.env2 = Environment(self.rng, gas_mv=320.0, temp_c=23.0, hum_pct=48.0)
            self.f2 = VirtualSensor(2, self.env2)
        self.start_s: Optional[float] = None
        self.ms = 0
        self.f1_offset = f1_boot_ms
        self.lastStatusMs = 0
        self.f1_boot_at_ms = 2000 if with_f2 else 0     # köprü önce açılır, F1 2 s sonra (gerçek kayıttaki gibi)

    @property
    def running(self) -> bool:
        return self.start_s is not None

    def start(self, now_s: float) -> None:
        self.start_s = now_s
        self.ms = 0
        if self.f2 is not None:
            self.emit_line(format_hello("gateway", 2))
            self._emit_local(self.f2.boot())

    def _emit_local(self, p: dict) -> None:
        self.emit_line(format_tel(node=2, via="local", ptype=p["ptype"], seq=p["seq"], up=p["up"],
                                  temp_x10=p["temp_x10"], hum_x10=p["hum_x10"], gas=p["gas"], base=p["base"],
                                  alarm=p["alarm"], flags=p["flags"]))

    def _f1_time(self) -> int:
        return self.f1_offset + self.ms - self.f1_boot_at_ms

    def _send_f1(self, p: Optional[dict]) -> None:
        if p is None:
            return
        ack = self.bridge.on_radio(p, self.ms)
        self.f1.handle_ack(ack)

    def tick(self, now_s: float) -> None:
        if self.start_s is None:
            return
        target = int((now_s - self.start_s) * 1000)
        while self.ms + STEP_MS <= target:
            self.ms += STEP_MS
            if self.ms >= self.f1_boot_at_ms:
                if not self.f1.booted:
                    self.f1.millis = self._f1_time()
                    self._send_f1(self.f1.boot(announce=self.with_f2))
                else:
                    self._send_f1(self.f1.step(self._f1_time()))
            if self.f2 is not None:
                p = self.f2.step(self.ms)
                if p is not None:
                    self._emit_local(p)
                if self.ms - self.lastStatusMs >= STATUS_PERIOD_MS:
                    self.lastStatusMs = self.ms
                    b = self.bridge
                    self.emit_line(format_gw(
                        up=self.ms, rx=b.rxCount, lost=b.lostCount, lq=b.lq(),
                        n1_link="never" if not b.seen else ("lost" if b.lost else "ok"),
                        n1_age_ms=(self.ms - b.lastRxMs) if b.seen else -1))
            self.bridge.check_link(self.ms)

    def trigger_lighter(self) -> bool:
        """Çakmak gazı. Döner: MQ ısınması atlandı mı."""
        skipped = self.f1.is_warming(self._f1_time())
        if skipped:
            self.f1.skip_warmup()
        self.env1.lighter(self._f1_time() / 1000.0)
        return skipped

    def scenario_finished(self) -> bool:
        age = self.env1.scenario_age(self._f1_time() / 1000.0)
        return age is not None and age >= SCENARIO_END_S and self.f1.alarmBits == 0

    def scenario_active(self) -> bool:
        age = self.env1.scenario_age(self._f1_time() / 1000.0)
        return age is not None and age < SCENARIO_END_S

    def reset_scenario(self) -> None:
        self.env1.clear()
        self.f1.clear_alarm_state()

    def command(self, line: str) -> None:
        self.bridge.command(line, self.f2)


class ReplaySource:
    """Kayıttaki satırları kayıttaki zamanlamayla oynatır.
    Biçimler: docs/ornek_seri_kayit.tsv ("<ms>\\t<json>") ya da oturum kaydı .jsonl ({"ms":…, "line":…})."""

    def __init__(self, path: Path, emit_line: Callable[[str], None], loop: bool = False) -> None:
        self.path = Path(path)
        self.emit_line = emit_line
        self.loop = loop
        self.items = load_recording(self.path)
        self.start_s: Optional[float] = None
        self.index = 0
        self.finished = False
        self.rounds = 0

    def start(self, now_s: float) -> None:
        self.start_s = now_s
        self.index = 0
        self.finished = False

    def tick(self, now_s: float) -> None:
        if self.start_s is None or self.finished:
            return
        elapsed_ms = (now_s - self.start_s) * 1000.0
        while self.index < len(self.items) and self.items[self.index][0] <= elapsed_ms:
            self.emit_line(self.items[self.index][1])
            self.index += 1
        if self.index >= len(self.items):
            self.rounds += 1
            if self.loop:
                self.start(now_s + 1.0)
            else:
                self.finished = True


def load_recording(path: Path) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        if "\t" in raw and raw.split("\t", 1)[0].strip().isdigit():
            ms, line = raw.split("\t", 1)
            items.append((int(ms), line))
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and "line" in obj and "ms" in obj:
            items.append((int(obj["ms"]), str(obj["line"])))
    items.sort(key=lambda it: it[0])
    return items
