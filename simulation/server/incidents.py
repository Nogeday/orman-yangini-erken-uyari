"""Olay akışı: alarm → olay → paket mesh'te ilerler → MERKEZ aldı → kurumlara (simüle) bildirim.

Zamanlayıcılar sunucudadır (tüm sekmeler aynı durumu görür) ve enjekte edilen saatle çalışır;
testler gerçek zamanı beklemez. Kurum bildirimleri SİMÜLASYONDUR: hiçbir kuruma ileti gitmez.
Gerçek olan tek bildirim, MERKEZ'in internet üzerinden ekibin Telegram kanalına gönderdiğidir
(Engine.telegram_notify → server/telegram_notify.py): olay MERKEZ'e ulaşınca "created",
alarm yükselince "escalated", operatör kapatınca "closed".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .protocol import alarm_name

if TYPE_CHECKING:  # pragma: no cover
    from .state import Engine, Origin

STATUS_TEXT = {
    "yolda": "Paket mesh'te ilerliyor",
    "rota_yok": "Rota yok — MERKEZ'e ulaşılamıyor",
    "merkez_aldi": "MERKEZ aldı",
}
DISPATCH_TEXT = {"bekliyor": "Bekliyor", "gonderiliyor": "Gönderiliyor…", "iletildi": "İletildi (simülasyon)"}


def _ms(t: float) -> int:
    return int(round(t * 1000))


@dataclass
class Incident:
    id: int
    source: str
    source_name: str
    started_at: float
    alarm: int
    alarm_peak: int
    simulated: bool
    tag: str
    lat: float
    lon: float
    rakim_m: int
    status: str = "yolda"
    route: Optional[list[str]] = None
    hops: int = 0
    lora_hops: int = 0
    rf_hops: int = 0
    latency_ms: float = 0.0
    received_at: Optional[float] = None
    source_normal_at: Optional[float] = None
    silenced_at: Optional[float] = None
    closed: bool = False
    dispatch: dict = field(default_factory=dict)
    telegram: Optional[dict] = None         # kanal bildiriminin durumu ("created" sonucu)
    reading: Optional[dict] = None          # olayı tetikleyen paket: gas, base, t, h (yalnızca fiziksel)
    propagation_id: Optional[int] = None
    can_silence: bool = False
    kinds: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "no": f"{self.id:03d}", "source": self.source, "source_name": self.source_name,
            "started_at": _ms(self.started_at), "alarm": self.alarm, "alarm_text": alarm_name(self.alarm),
            "alarm_peak": self.alarm_peak, "alarm_peak_text": alarm_name(self.alarm_peak),
            "simulated": self.simulated, "tag": self.tag,
            "lat": self.lat, "lon": self.lon, "rakim_m": self.rakim_m,
            "status": self.status, "status_text": STATUS_TEXT.get(self.status, self.status),
            "route": self.route, "hops": self.hops, "lora_hops": self.lora_hops, "rf_hops": self.rf_hops,
            "latency_ms": round(self.latency_ms, 1),
            "received_at": _ms(self.received_at) if self.received_at else None,
            "source_normal_at": _ms(self.source_normal_at) if self.source_normal_at else None,
            "silenced_at": _ms(self.silenced_at) if self.silenced_at else None,
            "dispatch": self.dispatch, "telegram": self.telegram, "can_silence": self.can_silence,
        }


class IncidentManager:
    def __init__(self, engine: "Engine", agencies: dict) -> None:
        self.engine = engine
        self.agencies = agencies.get("agencies", [])
        self.deliver_after_s = float(agencies.get("deliver_after_s", 0.8))
        self.incidents: dict[int, Incident] = {}
        self.propagations: dict[int, dict] = {}
        self._next_id = 1
        self._next_prop = 1
        self._epoch = 0

    # ---- sorgular ----
    def open_for(self, node_id: str) -> Optional[Incident]:
        for inc in self.incidents.values():
            if inc.source == node_id and not inc.closed:
                return inc
        return None

    def active(self) -> list[Incident]:
        return [i for i in self.incidents.values() if not i.closed]

    # ---- alarm girişi ----
    def on_alarm(self, node_id: str, alarm: int, prev: int, origin: "Origin", test: bool = False,
                 reading: Optional[dict] = None) -> None:
        e = self.engine
        inc = self.open_for(node_id)
        if alarm > 0 and inc is None and (prev == 0 or alarm > prev):
            self._create(node_id, alarm, origin, test, reading)
            return
        if inc is None:
            return
        if alarm > 0:
            inc.alarm = alarm
            if prev == 0:
                inc.source_normal_at = None
                e.log(f"Olay {inc.id:03d}: {node_id} alarmı yeniden başladı ({alarm_name(alarm)})", "alarm",
                      tag="TEST" if test else origin.tag)
            if alarm > inc.alarm_peak:
                old = inc.alarm_peak
                inc.alarm_peak = alarm
                e.log(f"Olay {inc.id:03d} güncellendi: alarm yükseldi {alarm_name(old)} → {alarm_name(alarm)}",
                      "alarm", origin)
                if inc.received_at is not None:     # MERKEZ almadıysa "created" mesajı zaten yeni alarmı taşır
                    e.telegram_notify(inc, "escalated", reading)
            e.emit("incident_updated", incident=inc.to_dict())
        elif prev > 0:
            inc.alarm = 0
            inc.source_normal_at = e.clock.wall()
            e.log(f"Olay {inc.id:03d}: kaynak normale döndü — olay operatör kapatana kadar açık", "info", origin)
            e.emit("incident_updated", incident=inc.to_dict())

    def on_silenced(self, node_id: str) -> None:
        inc = self.open_for(node_id)
        if inc is not None and inc.silenced_at is None:
            inc.silenced_at = self.engine.clock.wall()
            self.engine.emit("incident_updated", incident=inc.to_dict())

    def _create(self, node_id: str, alarm: int, origin: "Origin", test: bool = False,
                reading: Optional[dict] = None) -> None:
        e = self.engine
        n = e.cfg.node(node_id) or {}
        inc = Incident(
            id=self._next_id, source=node_id, source_name=n.get("ad", node_id), started_at=e.clock.wall(),
            alarm=alarm, alarm_peak=alarm, simulated=origin.simulated, tag="TEST" if test else origin.tag,
            lat=n.get("lat", 0.0), lon=n.get("lon", 0.0), rakim_m=n.get("rakim_m", 0),
            can_silence=n.get("tur") == "physical", reading=reading if n.get("tur") == "physical" else None,
        )
        self._next_id += 1
        inc.dispatch = {a["id"]: {"name": a["name"], "status": "bekliyor", "status_text": DISPATCH_TEXT["bekliyor"],
                                  "at": None} for a in self.agencies}
        self.incidents[inc.id] = inc
        e.log(f"Olay {inc.id:03d} açıldı: {node_id} {alarm_name(alarm)} · {inc.lat:.5f}, {inc.lon:.5f} · "
              f"{inc.rakim_m} m", "alarm", tag=inc.tag)
        self._plan_route(inc)
        e.emit("incident_created", incident=inc.to_dict())
        self._launch(inc)

    def _plan_route(self, inc: Incident) -> bool:
        r = self.engine.compute_route(inc.source)
        if r is None:
            inc.status = "rota_yok"
            inc.route, inc.hops, inc.lora_hops, inc.rf_hops, inc.latency_ms = None, 0, 0, 0, 0.0
            return False
        inc.status = "yolda"
        inc.route, inc.hops = r["path"], r["hops"]
        inc.lora_hops, inc.rf_hops, inc.latency_ms = r["lora_hops"], r["rf_hops"], r["latency_ms"]
        inc.kinds = r["kinds"]
        return True

    def _launch(self, inc: Incident) -> None:
        e = self.engine
        if inc.route is None:
            e.log(f"Olay {inc.id:03d}: rota yok — {inc.source} MERKEZ'e ulaşamıyor (devre dışı düğümler)", "warn",
                  tag=inc.tag)
            return
        r = {"path": inc.route, "hops": inc.hops, "kinds": inc.kinds}
        pid = self._next_prop
        self._next_prop += 1
        inc.propagation_id = pid
        prop = {"id": pid, "incident": inc.id, "path": r["path"], "kinds": r["kinds"], "hop_ms": e.hop_ms,
                "started_at": _ms(e.clock.wall()), "simulated": inc.simulated}
        self.propagations[pid] = prop
        e.emit("propagation", **prop)
        e.log(f"Paket yolda: {' → '.join(r['path'])} ({r['hops']} hop)", "info", tag=inc.tag)
        epoch = self._epoch
        e.scheduler.call_later(r["hops"] * e.hop_ms / 1000.0, self._received, inc.id, pid, epoch)

    def retry_waiting(self) -> None:
        """Topoloji değişince rotası olmayan olayları yeniden dener."""
        for inc in self.active():
            if inc.status == "rota_yok" and self._plan_route(inc):
                self.engine.emit("incident_updated", incident=inc.to_dict())
                self._launch(inc)

    def _alive(self, inc_id: int, epoch: int) -> Optional[Incident]:
        inc = self.incidents.get(inc_id)
        if epoch != self._epoch or inc is None or inc.closed:
            return None
        return inc

    def _received(self, inc_id: int, pid: int, epoch: int) -> None:
        inc = self._alive(inc_id, epoch)
        self.propagations.pop(pid, None)
        if inc is None:
            return
        e = self.engine
        inc.status = "merkez_aldi"
        inc.received_at = e.clock.wall()
        e.log(f"MERKEZ olayı aldı: Olay {inc.id:03d} ({inc.source} {alarm_name(inc.alarm_peak)})", "ok", tag=inc.tag)
        e.telegram_notify(inc, "created")       # MERKEZ → internet → ekibin kanalı (gerçek)
        e.emit("incident_received", incident=inc.to_dict())
        for a in self.agencies:
            e.scheduler.call_later(float(a.get("offset_s", 0.0)), self._dispatch, inc.id, a["id"], "gonderiliyor", epoch)

    def _dispatch(self, inc_id: int, agency_id: str, status: str, epoch: int) -> None:
        inc = self._alive(inc_id, epoch)
        if inc is None:
            return
        e = self.engine
        d = inc.dispatch[agency_id]
        d["status"], d["status_text"], d["at"] = status, DISPATCH_TEXT[status], _ms(e.clock.wall())
        e.emit("dispatch", incident=inc.id, agency=agency_id, status=status, status_text=d["status_text"], at=d["at"])
        e.log(f"{d['name']}: {d['status_text']} — Olay {inc.id:03d}", "ok" if status == "iletildi" else "info",
              tag=inc.tag)
        if status == "gonderiliyor":
            e.scheduler.call_later(self.deliver_after_s, self._dispatch, inc.id, agency_id, "iletildi", epoch)

    # ---- operatör ----
    def close(self, inc_id: int) -> Optional[Incident]:
        inc = self.incidents.get(inc_id)
        if inc is None or inc.closed:
            return None
        if inc.received_at is not None:         # kanala yalnızca duyurulmuş olay için "kapatıldı"
            self.engine.telegram_notify(inc, "closed")
        inc.closed = True
        if inc.propagation_id is not None:
            self.propagations.pop(inc.propagation_id, None)
        del self.incidents[inc_id]
        self.engine.log(f"Olay {inc.id:03d} kapatıldı (operatör)", "info", tag=inc.tag)
        self.engine.emit("incident_closed", id=inc.id)
        return inc

    def reset(self) -> None:
        self._epoch += 1          # bekleyen zamanlayıcılar geçersiz; numaralar yeniden 001'den
        self.incidents.clear()
        self.propagations.clear()
        self._next_id = 1
