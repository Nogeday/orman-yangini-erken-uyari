"""Telegram olay bildirimi (FAZ 2) — yalnızca ekibin kendi sohbetine / kanalına.

.env'de TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID yoksa sessizce kapalıdır. Gerçek kurumlara
(OGM / İtfaiye / Muhtarlık) hiçbir şey göndermez; kurum bildirimleri simülasyon olarak kalır.

Kurallar
- Olay akışını bekletmez: notify() arka planda görev başlatır ve hemen döner.
- İstisna fırlatmaz; sonuç on_result geri çağrısıyla SendResult olarak gelir.
- Token log'a, hata metnine ya da arayüze düşmez (httpx INFO logları kapatılır, metinler temizlenir).
- Aynı olay + tür bir kez gönderilir; reset() ("Sıfırla") bu hafızayı temizler.
- Bağlantı kurulamazsa bir kez, Telegram hız sınırında (≤ 5 s) bir kez yeniden dener.
- Grup süper gruba yükseltilip kimliği değişirse yeni kimliği izler ve log'da söyler.

Olay sözlüğü (eksik alanlar mesajda atlanır):
    id         olay no                              3
    node       kaynak düğüm                         "F1"
    node_type  "physical" | "virtual"
    simulated  simüle tetik mi                      False
    alarm      1 | 2 | 3  ya da  "GAZ" | "ISI" | "YANGIN"
    lat, lon   derece                               40.687512, 39.701234
    alt_m      rakım, m (rakim_m da kabul edilir)   1240
    at         olay zamanı: datetime, ISO metin ya da epoch (s / ms)
    route      ["F1", "F2", "N07", ..., "MERKEZ"]
    area       bölge adı
    reading    {"gas": 776, "base": 324, "t": 24.0, "h": 45.0}   (yalnızca fiziksel düğüm)

Kullanım (FastAPI):
    notifier = TelegramNotifier.from_env(SIM_DIR / ".env", on_result=yayinla)   # lifespan başında
    notifier.notify(olay, "created")     # "escalated": GAZ → YANGIN, "closed": operatör kapattı
    notifier.reset()                     # "Sıfırla"
    await notifier.aclose()              # kapanış
"""
from __future__ import annotations

import asyncio
import html
import inspect
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence, Union

import httpx

__all__ = [
    "TelegramNotifier", "SendResult", "Reply", "format_incident", "parse_chat_ids",
    "alarm_name", "format_time", "maps_url", "explain_error",
]

log = logging.getLogger("telegram")

# httpx her isteği INFO seviyesinde TAM URL ile loglar; Telegram URL'sinde token vardır.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org"
TRT = timezone(timedelta(hours=3), "TRT")  # Türkiye 2016'dan beri sabit UTC+3; Windows'ta tzdata gerekmez
ALARM_NAMES = {0: "NORMAL", 1: "GAZ", 2: "ISI", 3: "YANGIN"}
_ALARM_ALIASES = {"GAS": "GAZ", "HEAT": "ISI", "FIRE": "YANGIN"}
TITLES = {"created": "ALARM", "escalated": "ALARM YÜKSELDİ", "closed": "OLAY KAPATILDI"}
FOOTER = "Demo: OGM / İtfaiye / Muhtarlık bildirimleri simülasyondur."
USER_AGENT = "orman-yangin-mesh/1.0 (hackathon demo)"

_ID_KEYS = ("id", "no")
_NODE_KEYS = ("node", "source", "node_id")
_LAT_KEYS = ("lat", "latitude")
_LON_KEYS = ("lon", "lng", "longitude")
_ALT_KEYS = ("alt_m", "rakim_m", "altitude")
_TIME_KEYS = ("at", "created_at", "time", "ts")
_UPDATE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post",
                "my_chat_member", "chat_member", "chat_join_request")

_TOKEN_RE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,80}")
_CHAT_NUM = re.compile(r"-?\d{3,20}")
_CHAT_USER = re.compile(r"@[A-Za-z][A-Za-z0-9_]{4,31}")
_TME = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})/?",
                  re.IGNORECASE)

_ERROR_HINTS = (
    ("unauthorized", "Token geçersiz: @BotFather'dan aldığınız token'ı kontrol edin"),
    ("chat not found", "Sohbet bulunamadı: CHAT_ID yanlış, bot gruba/kanala eklenmemiş "
                       "ya da kişi bota /start göndermemiş"),
    ("bot was blocked", "Bot, kullanıcı tarafından engellenmiş"),
    ("bot was kicked", "Bot gruptan çıkarılmış"),
    ("bot is not a member", "Bot bu sohbetin üyesi değil (kanalda yönetici olmalı)"),
    ("can't initiate conversation", "Bot kişiye ilk mesajı atamaz: kişi önce bota /start göndermeli"),
    ("need administrator rights", "Bot kanalda 'Mesaj gönderme' yetkili yönetici olmalı"),
    ("not enough rights", "Botun bu sohbette mesaj gönderme yetkisi yok"),
    ("can't parse entities", "Mesaj biçimi (HTML) hatalı"),
    ("too many requests", "Telegram hız sınırı: biraz bekleyip tekrar deneyin"),
    ("terminated by other getupdates", "Aynı botu başka bir program dinliyor (getUpdates çakışması)"),
    ("webhook is active", "Botta webhook tanımlı: getUpdates kullanılamaz"),
)


# ---------------------------------------------------------------- yardımcılar

def _first(data: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _e(value: Any) -> str:
    """Telegram HTML metni için kaçış (yalnızca &, <, >)."""
    return html.escape(str(value), quote=False)


def _attr(value: str) -> str:
    """href özniteliği için kaçış."""
    return html.escape(value, quote=False).replace('"', "&quot;")


def alarm_name(value: Any) -> str:
    """1/2/3, "GAZ", "ısı", "fire"… → "GAZ" | "ISI" | "YANGIN"."""
    if value is None or isinstance(value, bool):
        return "ALARM"
    if isinstance(value, (int, float)):
        number = _num(value)
        return ALARM_NAMES.get(int(number), f"ALARM {int(number)}") if number is not None else "ALARM"
    text = str(value).strip().upper()
    if text.isdigit():
        return ALARM_NAMES.get(int(text), text)
    return _ALARM_ALIASES.get(text, text) or "ALARM"


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=TRT)).astimezone(TRT)
    number = _num(value) if not isinstance(value, str) else None
    if number is not None:
        if number >= 1e11:                      # epoch milisaniye
            return datetime.fromtimestamp(number / 1000, TRT)
        if number >= 1e9:                       # epoch saniye
            return datetime.fromtimestamp(number, TRT)
        return datetime.now(TRT)                # monotonic/uptime değeri: duvar saati değil
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text[-1] in "Zz":                    # Python 3.10 fromisoformat 'Z' kabul etmez
            text = text[:-1] + "+00:00"
        try:
            return _to_datetime(datetime.fromisoformat(text))
        except ValueError:
            number = _num(text)
            return _to_datetime(number) if number is not None else datetime.now(TRT)
    return datetime.now(TRT)


def format_time(value: Any = None) -> str:
    """Olay zamanı → '30.10.2026 18:52:07' (Türkiye saati). None → şimdi."""
    return _to_datetime(value).strftime("%d.%m.%Y %H:%M:%S")


def maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def explain_error(description: str) -> str:
    """Telegram'ın İngilizce hata açıklamasını kısa Türkçe ipucuna çevirir."""
    lowered = description.lower()
    for needle, hint in _ERROR_HINTS:
        if needle in lowered:
            return hint
    return description


def format_incident(incident: Mapping[str, Any], kind: str = "created") -> str:
    """Telegram HTML mesaj metni. Eksik alanlar atlanır; tüm metin HTML'e kaçırılır."""
    node = str(_first(incident, _NODE_KEYS) or "?")
    alarm = alarm_name(_first(incident, ("alarm", "alarm_type")))
    lines = [f"<b>{_e(TITLES.get(kind, 'OLAY'))}: {_e(node)} – {_e(alarm)}</b>"]

    head = []
    incident_id = _first(incident, _ID_KEYS)
    if incident_id is not None:
        head.append(f"Olay #{incident_id}")
    head.append(format_time(_first(incident, _TIME_KEYS)))
    lines.append(_e(" · ".join(head)))

    source = node
    node_type = str(_first(incident, ("node_type", "tur")) or "").lower()
    if node_type in ("physical", "fiziksel"):
        source += " · fiziksel düğüm"
    elif node_type in ("virtual", "sanal"):
        source += " · sanal düğüm"
    if incident.get("simulated"):
        source += " · SİMÜLE"
    lines.append(f"Kaynak: {_e(source)}")

    if kind != "closed":
        lat, lon = _num(_first(incident, _LAT_KEYS)), _num(_first(incident, _LON_KEYS))
        if lat is not None and lon is not None:
            position = f"{lat:.6f}, {lon:.6f}"
            altitude = _num(_first(incident, _ALT_KEYS))
            if altitude is not None:
                position += f" · rakım {altitude:.0f} m"
            lines.append(f"Konum: {_e(position)}")
            lines.append(f'<a href="{_attr(maps_url(lat, lon))}">Haritada aç</a>')

        route = _first(incident, ("route", "path"))
        if isinstance(route, (list, tuple)) and len(route) >= 2:
            lines.append(f"Rota: {_e(' → '.join(str(hop) for hop in route))} ({len(route) - 1} hop)")

        reading = _first(incident, ("reading", "telemetry"))
        if isinstance(reading, Mapping):
            parts = []
            gas, base = _num(reading.get("gas")), _num(reading.get("base"))
            if gas is not None:
                parts.append(f"gaz {gas:.0f} mV" + (f" (taban {base:.0f})" if base is not None else ""))
            temperature, humidity = _num(reading.get("t")), _num(reading.get("h"))
            if temperature is not None:
                parts.append(f"{temperature:.1f} °C")
            if humidity is not None:
                parts.append(f"nem %{humidity:.0f}")
            if parts:
                lines.append(f"Ölçüm: {_e(' · '.join(parts))}")

        area = _first(incident, ("area", "bolge", "region"))
        if area:
            lines.append(f"Bölge: {_e(area)}")

    lines.append(f"<i>{_e(FOOTER)}</i>")
    return "\n".join(lines)


def parse_chat_ids(value: Union[str, int, Sequence[Union[int, str]], None]
                   ) -> tuple[list[Union[int, str]], Optional[str]]:
    """'-100123, @kanal ; https://t.me/kanal' → ([-100123, '@kanal'], None). Geçersizler sorun metninde."""
    if value is None:
        items: list = []
    elif isinstance(value, str):
        items = value.replace(";", ",").split(",")
    elif isinstance(value, int) and not isinstance(value, bool):
        items = [value]
    else:
        items = list(value)

    chats: list[Union[int, str]] = []
    bad: list[str] = []
    for raw in items:
        text = str(raw).strip().strip("\"'")
        if not text:
            continue
        link = _TME.fullmatch(text)
        if _CHAT_NUM.fullmatch(text):
            chat: Union[int, str] = int(text)
        elif _CHAT_USER.fullmatch(text):
            chat = text
        elif link and link.group(1).lower() != "joinchat":
            chat = "@" + link.group(1)
        else:
            bad.append(text)
            continue
        if chat not in chats:
            chats.append(chat)

    problem = None
    if bad:
        problem = ("TELEGRAM_CHAT_ID geçersiz: " + ", ".join(bad)
                   + " (sayısal kimlik ya da @kanal_adi olmalı; davet bağlantısı kullanılamaz)")
    return chats, problem


def _load_env_file(path: Path) -> None:
    """KEY=VALUE satırlarını ortama ekler; ortamda zaten olan anahtarı ezmez."""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(path, override=False)
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key, value)


# ---------------------------------------------------------------- sonuç tipleri

@dataclass
class Reply:
    """Tek bir Bot API çağrısının sonucu (istisna yerine)."""
    ok: bool
    result: Any = None
    error: str = ""
    note: str = ""


@dataclass
class SendResult:
    """Bir olay bildiriminin sonucu. to_event() → WebSocket'e yayınlanacak sözlük."""
    incident_id: Any
    kind: str
    status: str               # "ok" | "partial" | "error"
    sent: int
    total: int
    detail: str               # hata nedeni ya da not (Türkçe, kısa)
    latency_ms: Optional[int]  # olaydan ilk mesajın teslimine kadar
    at: str                   # ISO zaman, Türkiye saati

    @classmethod
    def make(cls, incident_id: Any, kind: str, status: str, sent: int, total: int,
             detail: str, latency_ms: Optional[int]) -> "SendResult":
        return cls(incident_id, kind, status, sent, total, detail, latency_ms,
                   datetime.now(TRT).isoformat(timespec="seconds"))

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def ui_text(self) -> str:
        if self.status == "ok":
            text = "gönderildi"
            if self.latency_ms is not None:
                text += f" · {self.latency_ms / 1000:.1f} s"
            return text + (f" · {self.detail}" if self.detail else "")
        if self.status == "partial":
            return f"kısmen gönderildi ({self.sent}/{self.total}) · {self.detail}"
        return f"hata: {self.detail}"

    def to_event(self) -> dict:
        event = {"type": "telegram", **asdict(self)}
        event["text"] = self.ui_text
        return event


OnResult = Callable[[SendResult], Union[None, Awaitable[None]]]


# ---------------------------------------------------------------- bildirici

class TelegramNotifier:
    """Olayları ekibin Telegram sohbet(ler)ine gönderir. Ayrıntılar modül açıklamasında."""

    def __init__(self, token: str = "", chat_ids: Union[str, int, Sequence[Union[int, str]], None] = "",
                 *, send_pin: bool = True, on_result: Optional[OnResult] = None,
                 api_base: str = API_BASE, timeout_s: float = 6.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._token = (token or "").strip().strip("\"'")
        self.chat_ids, chat_problem = parse_chat_ids(chat_ids)
        self.send_pin = send_pin
        self.on_result = on_result
        self.api_base = api_base.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s, connect=min(3.0, timeout_s))
        self._transport = transport                                   # testler için
        self._sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep  # testler için
        self._sent_keys: set[tuple[str, str, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        try:
            self._loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self.problem = self._find_problem(chat_problem)
        self.enabled = self.problem is None and bool(self._token) and bool(self.chat_ids)

    @classmethod
    def from_env(cls, env_file: Union[str, os.PathLike, Sequence[Union[str, os.PathLike]], None] = None,
                 **kwargs: Any) -> "TelegramNotifier":
        """Ayarları ortamdan okur; env_file verilirse önce onu yükler (ortamdakini ezmez)."""
        if env_file is not None:
            files = [env_file] if isinstance(env_file, (str, os.PathLike)) else list(env_file)
            for file in files:
                _load_env_file(Path(file))
        pin = os.environ.get("TELEGRAM_SEND_PIN", "1").strip().lower()
        kwargs.setdefault("send_pin", pin not in ("0", "false", "no", "off", "hayir", "hayır"))
        kwargs.setdefault("api_base", os.environ.get("TELEGRAM_API_BASE") or API_BASE)
        return cls(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""), **kwargs)

    # -------------------------------------------------- durum

    @property
    def token_ok(self) -> bool:
        return bool(_TOKEN_RE.fullmatch(self._token))

    def _find_problem(self, chat_problem: Optional[str]) -> Optional[str]:
        if not self._token and not self.chat_ids and not chat_problem:
            return None                                  # hiç ayar yok → sessizce kapalı
        if not self._token:
            return "TELEGRAM_BOT_TOKEN boş"
        if not self.token_ok:
            return "TELEGRAM_BOT_TOKEN biçimi hatalı (beklenen: 123456789:AA…)"
        if chat_problem:
            return chat_problem
        if not self.chat_ids:
            return "TELEGRAM_CHAT_ID boş (bulmak için: tools/telegram_test.py --sohbetler)"
        return None

    def describe(self) -> str:
        """Açılış log satırı. Token ve kimlikler yazılmaz."""
        if self.enabled:
            return (f"Telegram: açık · {len(self.chat_ids)} sohbet"
                    + (" · konum iğnesi" if self.send_pin else ""))
        if self.problem:
            return f"Telegram: kapalı — {self.problem}"
        return "Telegram: kapalı (.env'de ayar yok)"

    # -------------------------------------------------- olay akışı

    def notify(self, incident: Mapping[str, Any], kind: str = "created") -> bool:
        """Bildirimi arka planda başlatır ve HEMEN döner (await etmeyin). Başlatıldıysa True.

        Olay döngüsünde çağrılmalı; başka bir thread'den çağrılırsa, döngü daha önce görüldüyse
        (nesne döngü içinde oluşturulduysa ya da notify döngüde çağrıldıysa) oraya aktarılır.
        """
        if not self.enabled:
            return False
        key = (str(_first(incident, _ID_KEYS)), kind, str(_first(incident, _TIME_KEYS)))
        if key in self._sent_keys:
            return False
        self._sent_keys.add(key)
        snapshot = dict(incident)
        try:
            loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._loop = loop
            self._spawn(snapshot, kind)
        elif self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._spawn, snapshot, kind)
        else:
            log.error("Telegram: olay döngüsü yok, bildirim atlandı (notify'ı sunucu içinden çağırın)")
            self._sent_keys.discard(key)
            return False
        return True

    def reset(self) -> None:
        """'Sıfırla': gönderilmiş olay hafızasını temizler."""
        self._sent_keys.clear()

    async def drain(self, timeout: Optional[float] = None) -> None:
        """Süren gönderimlerin bitmesini bekler (testler ve kapanış için)."""
        pending = set(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def aclose(self, timeout: float = 3.0) -> None:
        """Kapanış: süren gönderimlere kısa süre tanır, kalanları iptal eder."""
        pending = set(self._tasks)
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    async def send_incident(self, incident: Mapping[str, Any], kind: str = "created") -> SendResult:
        """Mesajı (olay yeni oluştuysa konum iğnesiyle) her sohbete gönderir. İstisna fırlatmaz."""
        started = time.monotonic()
        incident_id = _first(incident, _ID_KEYS)
        chats = list(self.chat_ids)
        if not self.enabled:
            return SendResult.make(incident_id, kind, "error", 0, len(chats), self.problem or "kapalı", None)

        errors: list[str] = []
        notes: list[str] = []
        delivered: list[tuple[Union[int, str], Any]] = []
        latency_ms: Optional[int] = None
        try:
            text = format_incident(incident, kind)
            async with self._client() as client:
                for chat in chats:
                    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                               "link_preview_options": {"is_disabled": True}}
                    reply = await self._call(client, "sendMessage", payload)
                    if reply.note:
                        notes.append(reply.note)
                    if reply.ok:
                        if latency_ms is None:
                            latency_ms = int((time.monotonic() - started) * 1000)
                        message = reply.result if isinstance(reply.result, dict) else {}
                        delivered.append((payload["chat_id"], message.get("message_id")))
                    else:
                        errors.append(f"{chat}: {reply.error}" if len(chats) > 1 else reply.error)

                lat, lon = _num(_first(incident, _LAT_KEYS)), _num(_first(incident, _LON_KEYS))
                if kind == "created" and self.send_pin and lat is not None and lon is not None:
                    for chat, message_id in delivered:
                        pin: dict[str, Any] = {"chat_id": chat, "latitude": lat, "longitude": lon,
                                               "disable_notification": True}
                        if message_id:
                            pin["reply_parameters"] = {"message_id": message_id,
                                                       "allow_sending_without_reply": True}
                        reply = await self._call(client, "sendLocation", pin)
                        if not reply.ok:
                            notes.append(f"konum iğnesi gitmedi: {reply.error}")
        except Exception as exc:  # beklenmeyen: olay akışına asla taşınmaz
            log.error("Telegram: beklenmeyen hata: %s", self._scrub(repr(exc)))
            log.debug("Telegram: ayrıntı", exc_info=True)
            message = f"beklenmeyen hata ({type(exc).__name__})"
            (notes if delivered and len(delivered) == len(chats) else errors).append(message)

        sent = len(delivered)
        status = "ok" if sent == len(chats) and not errors else ("partial" if sent else "error")
        detail = self._scrub("; ".join(dict.fromkeys(errors + notes)))
        result = SendResult.make(incident_id, kind, status, sent, len(chats), detail, latency_ms)
        log.log(logging.INFO if result.ok else logging.WARNING,
                "Telegram %s olay #%s: %s", kind, incident_id, result.ui_text)
        return result

    # -------------------------------------------------- test aracı için

    async def check_token(self) -> Reply:
        """getMe: token geçerli mi? result = bot bilgisi."""
        if not self.token_ok:
            return Reply(False, error="TELEGRAM_BOT_TOKEN boş ya da biçimi hatalı")
        async with self._client() as client:
            return await self._call(client, "getMe", {})

    async def list_recent_chats(self) -> tuple[Reply, list[dict]]:
        """Botun son 24 saatte gördüğü sohbetler (kimlik bulmak için). Kanal/grup önce."""
        if not self.token_ok:
            return Reply(False, error="TELEGRAM_BOT_TOKEN boş ya da biçimi hatalı"), []
        async with self._client() as client:
            reply = await self._call(client, "getUpdates", {"timeout": 0, "limit": 100})
        if not reply.ok:
            return reply, []
        found: dict[Any, dict] = {}
        migrated: set = set()
        for update in reply.result if isinstance(reply.result, list) else []:
            if not isinstance(update, dict):
                continue
            for key in _UPDATE_KEYS:
                obj = update.get(key)
                chat = obj.get("chat") if isinstance(obj, dict) else None
                if not isinstance(chat, dict) or "id" not in chat:
                    continue
                name = chat.get("title") or " ".join(
                    part for part in (chat.get("first_name"), chat.get("last_name")) if part)
                found[chat["id"]] = {"id": chat["id"], "type": chat.get("type", "?"),
                                     "title": name or "", "username": chat.get("username")}
                if obj.get("migrate_to_chat_id"):
                    migrated.add(chat["id"])
        rank = {"channel": 0, "supergroup": 1, "group": 2, "private": 3}
        chats = [chat for chat_id, chat in found.items() if chat_id not in migrated]
        return reply, sorted(chats, key=lambda chat: rank.get(chat["type"], 9))

    async def send_test_message(self) -> list[tuple[Union[int, str], Reply]]:
        """Her sohbete kısa bir düz metin test mesajı."""
        text = f"Bağlantı testi · Orman Yangını Erken Uyarı Ağı · {format_time()}"
        results = []
        async with self._client() as client:
            for chat in list(self.chat_ids):
                results.append((chat, await self._call(client, "sendMessage", {"chat_id": chat, "text": text})))
        return results

    # -------------------------------------------------- iç

    def _spawn(self, incident: dict, kind: str) -> None:
        task = asyncio.get_running_loop().create_task(self._run(incident, kind))
        self._tasks.add(task)                     # referans tutulmazsa görev çöpe gidebilir
        task.add_done_callback(self._tasks.discard)

    async def _run(self, incident: dict, kind: str) -> None:
        try:
            result = await self.send_incident(incident, kind)
        except Exception:                          # send_incident fırlatmaz; yine de emniyet
            log.error("Telegram: gönderim görevi çöktü")
            return
        if self.on_result is None:
            return
        try:
            outcome = self.on_result(result)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            log.exception("Telegram: on_result geri çağrısı hata verdi")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                 headers={"User-Agent": USER_AGENT})

    def _scrub(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "<token>")
        return _TOKEN_RE.sub("<token>", text)

    def _remap_chat(self, old: Union[int, str], new: Union[int, str]) -> None:
        remapped: list[Union[int, str]] = []
        for chat in self.chat_ids:
            chat = new if chat == old else chat
            if chat not in remapped:
                remapped.append(chat)
        self.chat_ids = remapped

    async def _call(self, client: httpx.AsyncClient, method: str, payload: dict) -> Reply:
        """Tek Bot API çağrısı. payload['chat_id'] süper grup göçünde yerinde güncellenir."""
        url = f"{self.api_base}/bot{self._token}/{method}"
        note = ""
        for attempt in (1, 2):
            try:
                response = await client.post(url, json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == 1:                  # istek hiç gitmedi: tekrar denemek güvenli
                    await self._sleep(1.0)
                    continue
                return Reply(False, error="api.telegram.org'a bağlanılamadı (internet?)", note=note)
            except httpx.TimeoutException:
                return Reply(False, error="zaman aşımı: Telegram yanıt vermedi", note=note)
            except httpx.HTTPError as exc:
                return Reply(False, error=self._scrub(f"ağ hatası ({type(exc).__name__})"), note=note)

            try:
                data = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return Reply(False, error=f"beklenmeyen yanıt (HTTP {response.status_code})", note=note)
            if data.get("ok"):
                return Reply(True, result=data.get("result"), note=note)

            description = str(data.get("description") or f"HTTP {response.status_code}")
            parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
            new_chat = parameters.get("migrate_to_chat_id")
            if attempt == 1 and new_chat and "chat_id" in payload:
                self._remap_chat(payload["chat_id"], new_chat)
                payload["chat_id"] = new_chat
                note = f"grup süper gruba yükseltilmiş: .env'de TELEGRAM_CHAT_ID={new_chat} yapın"
                log.warning("Telegram: %s", note)
                continue
            retry_after = _num(parameters.get("retry_after"))
            if attempt == 1 and retry_after is not None and retry_after <= 5:
                await self._sleep(retry_after)
                continue
            log.warning("Telegram %s başarısız: %s", method, self._scrub(description))
            return Reply(False, error=self._scrub(explain_error(description)), note=note)
        return Reply(False, error="yeniden deneme de başarısız oldu", note=note)
