"""Sistem durumu ve tek veri hattı.

Gerçek seri port, demo ve replay satırlarının hepsi `Engine.ingest_line` üzerinden aynı
ayrıştırıcıdan (protocol.parse_line), aynı tekrar elemeden ve aynı olay akışından geçer.
Engine tek iş parçacığında (asyncio döngüsü) çalışır; saat ve zamanlayıcı enjekte edilebilir.
"""
from __future__ import annotations

import heapq
import itertools
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .config import AppConfig
from .demo import DemoNetwork, ReplaySource
from .incidents import IncidentManager
from .mesh import hop_latency_ms, lora_airtime_for, shortest_path
from .protocol import (
    ALARM_NAMES, FLAG_DHT_ERR, FLAG_SILENCED, FLAG_TEST, FLAG_WARMUP, CommandResult, Deduper, DeviceLog,
    GatewayStatus, Hello, Help, Link, Pong, Telemetry, alarm_name, parse_line,
)

NODE_IDS = {1: "F1", 2: "F2"}
STALE_S = 3.5
HISTORY_S = 60.0
LOG_MAX = 200
STATUS_TEXT = {
    "NORMAL": "NORMAL", "GAZ": "GAZ", "ISI": "ISI", "YANGIN": "YANGIN",
    "BAGLANTI_YOK": "BAĞLANTI YOK", "DEVRE_DISI": "DEVRE DIŞI",
}


def _ms(t: Optional[float]) -> Optional[int]:
    return None if t is None else int(round(t * 1000))


# ---------------------------------------------------------------------------
#  Saat ve zamanlayıcı
# ---------------------------------------------------------------------------
class Clock:
    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class FakeClock(Clock):
    """Testler için elle ilerletilen saat."""

    def __init__(self, start: float = 1000.0, wall_start: float = 1_790_000_000.0) -> None:
        self.t = start
        self._offset = wall_start - start

    def now(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.t + self._offset

    def set(self, t: float) -> None:
        self.t = t

    def advance(self, dt: float) -> None:
        self.t += dt


class Scheduler:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._heap: list[tuple[float, int, Callable, tuple]] = []
        self._seq = itertools.count()

    def call_later(self, delay_s: float, fn: Callable, *args: Any) -> None:
        heapq.heappush(self._heap, (self.clock.now() + delay_s, next(self._seq), fn, args))

    def run_due(self) -> int:
        n = 0
        while self._heap and self._heap[0][0] <= self.clock.now() + 1e-9:
            _, _, fn, args = heapq.heappop(self._heap)
            fn(*args)
            n += 1
        return n

    def next_due(self) -> Optional[float]:
        return self._heap[0][0] if self._heap else None

    def clear(self) -> None:
        self._heap.clear()


# ---------------------------------------------------------------------------
#  Veri kaynağı etiketi
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Origin:
    kind: str           # serial | demo | sim | replay
    label: str = ""

    @property
    def simulated(self) -> bool:
        return self.kind in ("demo", "sim")

    @property
    def tag(self) -> str:
        if self.simulated:
            return "SİMÜLE"
        return "REPLAY" if self.kind == "replay" else ""


SIM = Origin("sim", "SİMÜLE")


class SessionLog:
    """Her ham satır → simulation/data/logs/oturum-YYYYMMDD-HHMMSS.jsonl (zaman damgasıyla).
    Bu dosya `--source replay --replay-file …` ile yeniden oynatılabilir."""

    def __init__(self, logs_dir: Path, clock: Clock) -> None:
        self.logs_dir = Path(logs_dir)
        self.clock = clock
        self.path: Optional[Path] = None
        self._f = None
        self._t0 = clock.now()

    def write(self, line: str, origin: Origin) -> None:
        if self._f is None:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            self.path = self.logs_dir / time.strftime("oturum-%Y%m%d-%H%M%S.jsonl")
            self._f = open(self.path, "a", encoding="utf-8", buffering=1)
        wall = self.clock.wall()
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall)) + f".{int(wall * 1000) % 1000:03d}",
            "ms": int((self.clock.now() - self._t0) * 1000),
            "src": f"{origin.kind}:{origin.label}" if origin.label else origin.kind,
            "line": line.rstrip("\r\n"),
        }
        try:
            self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


# ---------------------------------------------------------------------------
#  Düğüm durumu
# ---------------------------------------------------------------------------
class NodeState:
    def __init__(self, rec: dict) -> None:
        self.rec = rec
        self.id: str = rec["id"]
        self.tur: str = rec["tur"]
        self.alarm = 0
        self.flags = 0
        self.tel: Optional[dict] = None
        self.seen = False
        self.last_seen: Optional[float] = None
        self.last_seen_wall: Optional[float] = None
        self.link_lost = False
        self.link_ok_age_ms: Optional[int] = None
        self.stale = False
        self.origin_kind = ""
        self.disabled = False
        self.sim_fire = False
        self.hello: Optional[dict] = None
        self.gw: Optional[dict] = None
        self.history: deque[tuple[int, int, int]] = deque()

    @property
    def physical(self) -> bool:
        return self.tur == "physical"

    def lost(self) -> bool:
        return self.physical and (not self.seen or self.link_lost or self.stale)

    def status(self) -> str:
        if self.tur == "virtual":
            if self.disabled:
                return "DEVRE_DISI"
            return "YANGIN" if self.sim_fire else "NORMAL"
        if self.tur == "center":
            return "NORMAL"
        if self.lost():
            return "BAGLANTI_YOK"
        return ALARM_NAMES.get(self.alarm, "NORMAL")

    def badges(self) -> list[str]:
        out = []
        if not self.physical or not self.seen:
            if self.sim_fire:
                out.append("SİMÜLE")
            return out
        if self.flags & FLAG_WARMUP:
            out.append("ISINIYOR")
        if self.flags & FLAG_SILENCED:
            out.append("SUSTURULDU")
        if self.flags & FLAG_TEST:
            out.append("TEST")
        if self.flags & FLAG_DHT_ERR:
            out.append("DHT HATASI")
        if self.origin_kind in ("demo", "sim"):
            out.append("SİMÜLE")
        elif self.origin_kind == "replay":
            out.append("REPLAY")
        return out

    def to_dict(self) -> dict:
        st = self.status()
        return {
            "id": self.id, "tur": self.tur, "status": st, "status_text": STATUS_TEXT[st],
            "alarm": self.alarm, "alarm_text": alarm_name(self.alarm), "flags": self.flags,
            "badges": self.badges(), "seen": self.seen, "lost": self.lost(),
            "link_lost": self.link_lost, "stale": self.stale,
            "last_seen": _ms(self.last_seen_wall), "tel": self.tel, "gw": self.gw, "hello": self.hello,
            "origin": self.origin_kind, "disabled": self.disabled, "sim_fire": self.sim_fire,
        }


# ---------------------------------------------------------------------------
#  Motor
# ---------------------------------------------------------------------------
Emit = Callable[[dict], None]


class Engine:
    def __init__(self, cfg: AppConfig, clock: Optional[Clock] = None, emit: Optional[Emit] = None,
                 notifier: Any = None, session_log: Optional[SessionLog] = None, demo_seed: int = 7) -> None:
        self.cfg = cfg
        self.clock = clock or Clock()
        self.scheduler = Scheduler(self.clock)
        self._emit = emit or (lambda ev: None)
        self.notifier = notifier
        self.session_log = session_log
        self.demo_seed = demo_seed
        self.nodes: dict[str, NodeState] = {n["id"]: NodeState(n) for n in cfg.nodes}
        self.links = cfg.links
        self.link_kind = {frozenset((l.a, l.b)): l.kind for l in self.links}
        self.dedupe = Deduper()
        self.logs: deque[dict] = deque(maxlen=LOG_MAX)
        self._log_seq = itertools.count(1)
        self.incidents = IncidentManager(self, cfg.agencies)
        mesh = cfg.area.mesh
        self.hop_ms = mesh.hop_animation_ms
        self.lora = mesh.lora_params()
        self.airtime_ms = lora_airtime_for(self.lora)
        self.hop_latency_ms = hop_latency_ms(self.lora)
        self.mode = "starting"
        self.source: dict = {"mode": "starting", "label": "Başlatılıyor…", "state": "warn", "message": ""}
        self.demo: Optional[DemoNetwork] = None
        self.overlay: Optional[DemoNetwork] = None
        self.replay: Optional[ReplaySource] = None
        self.command_sink: Optional[Callable[[str], bool]] = None
        self.route = self.compute_route("F1")

    # ---- yayın ve kayıt ----
    def emit(self, type_: str, **payload: Any) -> None:
        self._emit({"type": type_, **payload})

    def log(self, text: str, level: str = "info", origin: Optional[Origin] = None, tag: Optional[str] = None) -> None:
        entry = {"id": next(self._log_seq), "at": _ms(self.clock.wall()), "text": text, "level": level,
                 "tag": tag if tag is not None else (origin.tag if origin else "")}
        self.logs.append(entry)
        self.emit("log", entry=entry)

    def set_source(self, mode: str, label: str, state: str = "ok", message: str = "", log: bool = True) -> None:
        changed = (mode, label, message) != (self.source.get("mode"), self.source.get("label"), self.source.get("message"))
        self.mode = mode
        self.source = {"mode": mode, "label": label, "state": state, "message": message}
        if changed:
            self.emit("source_status", source=self.source)
            if log and (message or label):
                self.log(message or label, "warn" if state != "ok" else "info", tag="")

    # ---- rota ----
    def disabled(self) -> set[str]:
        return {n.id for n in self.nodes.values() if n.disabled}

    def compute_route(self, src: str) -> Optional[dict]:
        sp = shortest_path(self.links, src, "MERKEZ", self.disabled())
        if sp is None:
            return None
        path, cost = sp
        kinds = [self.link_kind.get(frozenset((a, b)), "lora") for a, b in zip(path, path[1:])]
        lora_hops = sum(1 for k in kinds if k == "lora")
        return {"path": path, "hops": len(path) - 1, "cost": round(cost, 3), "kinds": kinds,
                "lora_hops": lora_hops, "rf_hops": len(kinds) - lora_hops,
                "latency_ms": round(lora_hops * self.hop_latency_ms, 1)}

    # ---- veri hattı ----
    def ingest_line(self, line: str, origin: Origin, rx_wall: Optional[float] = None) -> None:
        if self.session_log is not None:
            self.session_log.write(line, origin)
        msg = parse_line(line)
        if msg is None:
            return
        now = self.clock.now()
        rx_wall = rx_wall if rx_wall is not None else self.clock.wall()
        if isinstance(msg, Telemetry) and self.dedupe.is_duplicate(msg, now):
            return
        if self.overlay is not None and origin.kind != "sim" and getattr(msg, "node", None) == 1 \
                and isinstance(msg, (Telemetry, Link)):
            # "Simüle Çakmak Tetikle" sürerken F1'i sanal F1 sürer; gerçek F1 alarmı her zaman önceliklidir.
            if isinstance(msg, Telemetry) and msg.alarm > 0:
                self.stop_overlay("Gerçek F1 alarmı geldi — simülasyon durduruldu")
            else:
                return
        if isinstance(msg, Telemetry):
            self._on_tel(msg, origin, now, rx_wall)
        elif isinstance(msg, Link):
            self._on_link(msg, origin, now, rx_wall)
        elif isinstance(msg, GatewayStatus):
            self._on_gw(msg)
        elif isinstance(msg, Hello):
            self._on_hello(msg, origin)
        elif isinstance(msg, CommandResult):
            ok = "kabul" if msg.ok else "RED"
            self.log(f"Köprü yanıtı: {msg.cmd} {msg.target} — {ok} ({msg.note})", "info" if msg.ok else "warn", origin)
        elif isinstance(msg, DeviceLog):
            name = NODE_IDS.get(msg.node, f"düğüm {msg.node}")
            level = {"error": "warn", "warn": "warn"}.get(msg.level, "info")
            self.log(f"{name} cihaz kaydı [{msg.level}]: {msg.msg}", level, origin)
        elif isinstance(msg, Pong):
            self.log(f"Köprü: pong (açık {msg.up / 1000:.0f} s)", "info", origin)
        elif isinstance(msg, Help):
            self.log(f"Köprü komutları: {msg.cmds}", "info", origin)

    def _on_tel(self, msg: Telemetry, origin: Origin, now: float, rx_wall: float) -> None:
        nid = NODE_IDS.get(msg.node)
        if nid is None or nid not in self.nodes:
            return
        n = self.nodes[nid]
        was_lost = n.seen and n.lost()
        prev_alarm = n.alarm if n.seen else 0
        prev_flags = n.flags if n.seen else 0
        prev_status = n.status()
        n.seen = True
        n.stale = False
        n.link_lost = False
        n.alarm, n.flags = msg.alarm, msg.flags
        n.last_seen, n.last_seen_wall = now, rx_wall
        n.origin_kind = origin.kind
        n.tel = {"via": msg.via, "kind": msg.kind, "seq": msg.seq, "up": msg.up, "t": msg.t, "h": msg.h,
                 "gas": msg.gas, "base": msg.base, "vbat": msg.vbat, "txf": msg.txf, "lq": msg.lq, "rf_ok": msg.rf_ok}
        wall_ms = _ms(rx_wall)
        n.history.append((wall_ms, msg.gas, msg.base))
        while n.history and n.history[0][0] < wall_ms - HISTORY_S * 1000:
            n.history.popleft()

        if msg.kind == "boot":
            self.log(f"{nid} açıldı" + (" — MQ ısınıyor (45 s gaz alarmı yok)" if msg.flags & FLAG_WARMUP else ""),
                     "info", origin)
        elif was_lost:
            gap = f" (köprü: {n.link_ok_age_ms} ms kesinti)" if n.link_ok_age_ms is not None else ""
            self.log(f"{nid} verisi geri geldi{gap}", "ok", origin)
        n.link_ok_age_ms = None
        if prev_flags & FLAG_WARMUP and not msg.flags & FLAG_WARMUP and msg.kind != "boot":
            self.log(f"{nid}: MQ ısınması bitti, gaz alarmı etkin", "info", origin)
        if msg.flags & FLAG_SILENCED and not prev_flags & FLAG_SILENCED:
            self.log(f"{nid} onayladı: siren susturuldu", "ok", origin)
            self.incidents.on_silenced(nid)
        if msg.flags & FLAG_TEST and not prev_flags & FLAG_TEST:
            self.log(f"{nid}: TEST alarmı (BOOT butonu)", "warn", origin)
        if msg.flags & FLAG_DHT_ERR and not prev_flags & FLAG_DHT_ERR:
            self.log(f"{nid}: DHT11 okunamıyor (sıcaklık/nem yok)", "warn", origin)

        test = bool(msg.flags & FLAG_TEST)
        if msg.alarm != prev_alarm:
            if msg.alarm and not prev_alarm:
                delta = msg.gas - msg.base
                self.log(f"{nid} {alarm_name(msg.alarm)} alarmı — gaz {msg.gas} mV (tabandan {delta:+d} mV), "
                         f"sıcaklık {_fmt_t(msg.t)}" + (" · BOOT butonu test alarmı" if test else ""),
                         "alarm", origin, tag="TEST" if test and not origin.tag else None)
            elif msg.alarm:
                self.log(f"{nid} alarm değişti: {alarm_name(prev_alarm)} → {alarm_name(msg.alarm)} "
                         f"(sıcaklık {_fmt_t(msg.t)})", "alarm", origin)
            else:
                self.log(f"{nid} normale döndü", "ok", origin)
        node = n.to_dict()
        self.emit("telemetry", node=node, point=[wall_ms, msg.gas, msg.base], rx_at=wall_ms)
        if n.status() != prev_status or msg.alarm != prev_alarm:
            self.emit("node_state", node=node, rx_at=wall_ms)
        if msg.alarm != prev_alarm:
            self.incidents.on_alarm(nid, msg.alarm, prev_alarm, origin, test=test)

    def _on_link(self, msg: Link, origin: Origin, now: float, rx_wall: float) -> None:
        nid = NODE_IDS.get(msg.node)
        if nid is None or nid not in self.nodes:
            return
        n = self.nodes[nid]
        if msg.state == "lost":
            already = n.lost()
            n.link_lost = True
            if not already:
                self.log(f"{nid} bağlantısı koptu — köprü {msg.age_ms} ms paket almadı", "warn", origin)
        else:
            n.link_lost = False
            n.link_ok_age_ms = msg.age_ms     # "geri geldi" kaydı ilk veriyle birlikte yazılır
        self.emit("link", node=nid, state=msg.state, age_ms=msg.age_ms)
        self.emit("node_state", node=n.to_dict(), rx_at=_ms(rx_wall))

    def _on_gw(self, msg: GatewayStatus) -> None:
        n = self.nodes.get("F2")
        if n is None:
            return
        n.gw = {"radio": msg.radio, "ch": msg.ch, "rx": msg.rx, "lost": msg.lost, "lq": msg.lq,
                "n1_link": msg.n1_link, "n1_age_ms": msg.n1_age_ms, "up": msg.up}
        self.emit("telemetry", node=n.to_dict(), point=None, rx_at=None)

    def _on_hello(self, msg: Hello, origin: Origin) -> None:
        nid = NODE_IDS.get(msg.node, f"düğüm {msg.node}")
        if nid in self.nodes:
            self.nodes[nid].hello = {"role": msg.role, "fw": msg.fw, "proto": msg.proto, "radio": msg.radio, "ch": msg.ch}
        role = "köprü" if msg.role == "gateway" else ("saha düğümü (doğrudan USB)" if msg.role == "sensor" else msg.role)
        radio = f"radyo hazır, kanal {msg.ch}" if msg.radio else "nRF24 BULUNAMADI — kablo/3V3/kondansatör"
        self.log(f"{nid} merhaba: {role} · fw {msg.fw} · protokol {msg.proto} · {radio}",
                 "info" if msg.radio else "warn", origin)
        if msg.proto != 1:
            self.log(f"{nid}: protokol sürümü {msg.proto} — sunucu 1 bekliyor", "warn", origin)

    # ---- periyodik ----
    def tick(self) -> None:
        now = self.clock.now()
        for src in (self.demo, self.overlay, self.replay):
            if src is not None:
                src.tick(now)
        if self.replay is not None and self.replay.finished:
            self.log("Replay bitti", "info", tag="REPLAY")
            self.replay = None
        self.scheduler.run_due()
        for n in self.nodes.values():
            if n.physical and n.seen and not n.stale and n.last_seen is not None and now - n.last_seen > STALE_S:
                already = n.lost()
                n.stale = True
                if not already:
                    self.log(f"{n.id}: {STALE_S} s veri yok — BAĞLANTI YOK", "warn",
                             tag="SİMÜLE" if n.origin_kind in ("demo", "sim") else "")
                self.emit("node_state", node=n.to_dict(), rx_at=None)
        if self.overlay is not None and self.overlay.scenario_finished():
            self.stop_overlay("Simülasyon bitti — F1 yeniden gerçek veriyle izleniyor")

    # ---- kaynaklar ----
    def start_demo(self, label: str = "DEMO", message: str = "") -> None:
        if self.demo is None:
            origin = Origin("demo", "DEMO")
            self.demo = DemoNetwork(lambda line: self.ingest_line(line, origin), seed=self.demo_seed)
            self.demo.start(self.clock.now())
        self.command_sink = None
        self.set_source("demo", label, "ok", message)

    def stop_demo(self) -> None:
        self.demo = None

    def start_replay(self, path: Path, loop: bool = False) -> None:
        origin = Origin("replay", Path(path).name)
        self.replay = ReplaySource(Path(path), lambda line: self.ingest_line(line, origin), loop=loop)
        self.replay.start(self.clock.now())
        self.set_source("replay", f"REPLAY: {Path(path).name}", "ok",
                        f"Replay başladı: {Path(path).name} ({len(self.replay.items)} satır)")

    def stop_overlay(self, reason: str) -> None:
        if self.overlay is not None:
            self.overlay = None
            self.log(reason, "info", tag="SİMÜLE")

    # ---- operatör eylemleri ----
    def trigger_lighter(self) -> dict:
        if self.demo is not None:
            skipped = self.demo.trigger_lighter()
            target = "sanal F1"
        else:
            if self.overlay is None:
                last_up = (self.nodes["F1"].tel or {}).get("up") or 600_000
                self.overlay = DemoNetwork(lambda line: self.ingest_line(line, SIM), seed=self.demo_seed + 1,
                                           with_f2=False, f1_boot_ms=int(last_up) + 5_000, f1_warmup=False)
                self.overlay.start(self.clock.now())
            skipped = self.overlay.trigger_lighter()
            target = "sanal F1 (donanım verisinin yerine geçici)"
        self.log(f"Simüle çakmak gazı → {target}", "warn", tag="SİMÜLE")
        if skipped:
            self.log("Sanal F1: MQ ısınması simülasyon için atlandı", "info", tag="SİMÜLE")
        return {"ok": True, "message": "Simüle çakmak tetiklendi"}

    def reset(self) -> dict:
        self.incidents.reset()
        self.scheduler.clear()
        for n in self.nodes.values():
            n.disabled = False
            n.sim_fire = False
        if self.demo is not None:
            self.demo.reset_scenario()
        self.overlay = None
        self.route = self.compute_route("F1")
        self.log("Sıfırlandı: olaylar, animasyonlar ve devre dışı düğümler temizlendi", "info")
        self.emit("reset")
        self.emit("snapshot", **self.snapshot())
        return {"ok": True, "message": "Sıfırlandı"}

    def command(self, target: str, cmd: str) -> dict:
        target = str(target).lower()
        if cmd not in ("silence", "test") or target not in ("1", "2", "all"):
            return {"ok": False, "message": "geçersiz komut"}
        line = f"{cmd} {target}"
        if self.overlay is not None and target in ("1", "all"):
            self.log(f"Komut → sanal köprü: {line}", "info", tag="SİMÜLE")
            self.overlay.command(line)
            return {"ok": True, "message": f"{line} (simüle)"}
        if self.demo is not None:
            self.log(f"Komut → sanal köprü: {line}", "info", tag="SİMÜLE")
            self.demo.command(line)
            return {"ok": True, "message": f"{line} (demo)"}
        if self.command_sink is not None and self.command_sink(line):
            self.log(f"Komut → köprü (USB): {line}", "info")
            return {"ok": True, "message": line}
        self.log(f"Komut gönderilemedi ({line}): bağlı köprü yok", "warn")
        return {"ok": False, "message": "bağlı köprü yok"}

    def toggle_node(self, node_id: str) -> dict:
        n = self.nodes.get(node_id)
        if n is None or n.tur != "virtual":
            return {"ok": False, "message": "yalnızca sanal düğümler devre dışı bırakılabilir"}
        n.disabled = not n.disabled
        self.emit("node_state", node=n.to_dict(), rx_at=None)
        self.route = self.compute_route("F1")
        verb = "devre dışı bırakıldı" if n.disabled else "yeniden etkin"
        if self.route:
            self.log(f"{node_id} {verb}. Rota yeniden hesaplandı: {' → '.join(self.route['path'])} "
                     f"({self.route['hops']} hop)", "info")
        else:
            self.log(f"{node_id} {verb}. Rota yok: F1 MERKEZ'e ulaşamıyor", "warn")
        self.emit("route", route=self.route, disabled=sorted(self.disabled()))
        self.incidents.retry_waiting()
        return {"ok": True, "message": verb, "route": self.route}

    def simulate_fire(self, node_id: str) -> dict:
        n = self.nodes.get(node_id)
        if n is None or n.tur != "virtual":
            return {"ok": False, "message": "yalnızca sanal düğümlerde"}
        if n.disabled:
            return {"ok": False, "message": "düğüm devre dışı"}
        if n.sim_fire:
            return {"ok": False, "message": "bu düğümde zaten simüle yangın var"}
        n.sim_fire = True
        self.log(f"{node_id}: yangın simülasyonu başlatıldı", "alarm", tag="SİMÜLE")
        self.emit("node_state", node=n.to_dict(), rx_at=None)
        self.incidents.on_alarm(node_id, 3, 0, SIM)
        return {"ok": True, "message": "simüle yangın"}

    def close_incident(self, inc_id: int) -> dict:
        inc = self.incidents.close(inc_id)
        if inc is None:
            return {"ok": False, "message": "olay bulunamadı"}
        n = self.nodes.get(inc.source)
        if n is not None and n.sim_fire:
            n.sim_fire = False
            self.emit("node_state", node=n.to_dict(), rx_at=None)
        return {"ok": True, "message": "olay kapatıldı"}

    # ---- anlık görüntü ----
    def snapshot(self) -> dict:
        return {
            "server_time": _ms(self.clock.wall()),
            "source": self.source,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "route": self.route,
            "disabled": sorted(self.disabled()),
            "incidents": [i.to_dict() for i in self.incidents.active()],
            "propagations": list(self.incidents.propagations.values()),
            "log": list(self.logs),
            "history": {nid: [list(p) for p in n.history] for nid, n in self.nodes.items() if n.physical},
        }


def _fmt_t(t: Optional[float]) -> str:
    return "—" if t is None else f"{t:.1f} °C"
