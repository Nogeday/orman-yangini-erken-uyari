"""İsteğe bağlı Telegram bildirimi — yalnızca EKİBİN KENDİ sohbetine.

simulation/.env içinde TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID varsa, MERKEZ bir olayı aldığında
mesaj gönderilir; sonuç arayüzde "Telegram: gönderildi / hata" olarak görünür. Değişkenler yoksa
özellik sessizce kapalıdır. Gerçek kurumlara (OGM, itfaiye, muhtarlık) hiçbir ileti gönderilmez.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:  # pragma: no cover
    from .state import Engine

API = "https://api.telegram.org/bot{token}/sendMessage"


def format_message(inc: dict, area_title: str) -> str:
    lat, lon = inc["lat"], inc["lon"]
    when = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(inc["started_at"] / 1000))
    tag = " (SİMÜLE)" if inc.get("simulated") else ""
    route = " → ".join(inc.get("route") or [])
    return "\n".join([
        f"ORMAN YANGINI ERKEN UYARI — Olay {inc['no']}{tag}",
        f"Alarm: {inc['alarm_peak_text']}",
        f"Kaynak: {inc['source_name']}",
        f"Bölge: {area_title}",
        f"Koordinat: {lat:.5f}, {lon:.5f} · rakım {inc['rakim_m']} m",
        f"Harita: https://www.google.com/maps?q={lat:.6f},{lon:.6f}",
        f"Saat: {when}",
        f"Rota: {route} ({inc.get('hops', 0)} hop)" if route else "Rota: —",
        "Not: hackathon demosu; kurum bildirimleri simülasyondur.",
    ])


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, area_title: str, client: Optional[httpx.AsyncClient] = None) -> None:
        self.token = token
        self.chat_id = chat_id
        self.area_title = area_title
        self._client = client
        self.engine: Optional["Engine"] = None
        self._tasks: set[asyncio.Task] = set()

    def bind(self, engine: "Engine") -> None:
        self.engine = engine

    def incident_received(self, inc: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._send(inc))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, inc: dict) -> None:
        ok, detail = False, ""
        client = self._client or httpx.AsyncClient(timeout=8.0)
        try:
            r = await client.post(API.format(token=self.token),
                                  json={"chat_id": self.chat_id, "text": format_message(inc, self.area_title),
                                        "disable_web_page_preview": True})
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            ok = r.status_code == 200 and bool(data.get("ok"))
            detail = "" if ok else str(data.get("description") or f"HTTP {r.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            detail = type(exc).__name__
        finally:
            if self._client is None:
                await client.aclose()
        if self.engine is not None:
            self.engine.incidents.set_notify(inc["id"], "telegram", ok, detail)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()


def make_notifier(env: dict[str, str], area_title: str) -> Optional[TelegramNotifier]:
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return None
    return TelegramNotifier(token, chat, area_title)
