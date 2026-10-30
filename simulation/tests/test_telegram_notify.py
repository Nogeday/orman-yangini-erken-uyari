"""Telegram bildirimi testleri — ağ yok; api.telegram.org httpx.MockTransport ile taklit edilir."""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.telegram_notify import (  # noqa: E402
    TelegramNotifier, alarm_name, format_incident, format_time, parse_chat_ids,
)

TOKEN = "123456789:AAH-test_token_abcdefghijklmnopqrstuv"
GROUP = -1001234567890

INCIDENT = {
    "id": 3, "node": "F1", "node_type": "physical", "simulated": False, "alarm": 1,
    "lat": 40.687512, "lon": 39.701234, "alt_m": 1240.4,
    "at": "2026-10-30T15:52:07+00:00",
    "route": ["F1", "F2", "N07", "N03", "MERKEZ"],
    "area": "Maçka <Altındere> & Sümela",
    "reading": {"gas": 776, "base": 324, "t": 24.0, "h": 45.0},
}


class FakeTelegram:
    """api.telegram.org taklidi: istekleri kaydeder; programlanmış yanıt yoksa başarı döner."""

    def __init__(self, script=None):
        self.calls = []          # (method, body, url)
        self.script = script or {}
        self.message_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body, str(request.url)))
        queue = self.script.get(method)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            status, payload = item
            return httpx.Response(status, json=payload)
        self.message_id += 1
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": self.message_id, "chat": {"id": body.get("chat_id")}}})

    def methods(self):
        return [call[0] for call in self.calls]


def make(fake, chats=str(GROUP), **kwargs):
    notifier = TelegramNotifier(TOKEN, chats, transport=httpx.MockTransport(fake.handler), **kwargs)

    async def no_sleep(_seconds):
        return None

    notifier._sleep = no_sleep
    return notifier


def error(code, description, **parameters):
    body = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return code, body


# ------------------------------------------------------------------ yapılandırma

def test_ayar_yoksa_sessizce_kapali():
    notifier = TelegramNotifier("", "")
    assert not notifier.enabled and notifier.problem is None
    assert notifier.describe() == "Telegram: kapalı (.env'de ayar yok)"
    assert notifier.notify(INCIDENT) is False          # döngü gerekmeden hemen döner


def test_yapilandirma_hatalari_acikca_soylenir():
    assert "biçimi hatalı" in TelegramNotifier("abc", str(GROUP)).problem
    assert "TELEGRAM_CHAT_ID boş" in TelegramNotifier(TOKEN, "").problem
    assert "davet" in TelegramNotifier(TOKEN, "https://t.me/+AbCdEf123").problem
    assert not TelegramNotifier(TOKEN, "").enabled


def test_sohbet_kimlikleri_ayristirilir():
    chats, problem = parse_chat_ids(' -1001234567890 , @orman_yangin_demo ; "https://t.me/orman_demo_kanal" ')
    assert problem is None
    assert chats == [-1001234567890, "@orman_yangin_demo", "@orman_demo_kanal"]
    assert parse_chat_ids("123456789,123456789")[0] == [123456789]


def test_from_env_dosyadan_okur_ortami_ezmez(tmp_path, monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_SEND_PIN", "TELEGRAM_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"# yorum\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={GROUP},@orman_yangin_demo\n"
                   "TELEGRAM_SEND_PIN=0\n", encoding="utf-8")
    notifier = TelegramNotifier.from_env(env)
    assert notifier.enabled and notifier.chat_ids == [GROUP, "@orman_yangin_demo"]
    assert notifier.send_pin is False
    assert notifier.describe() == "Telegram: açık · 2 sohbet"

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")          # testlerde kapatmanın yolu: boş değer
    assert not TelegramNotifier.from_env(env).enabled


# ------------------------------------------------------------------ mesaj biçimi

def test_mesaj_bicimi_html_kacisli():
    text = format_incident(INCIDENT)
    assert text.startswith("<b>ALARM: F1 – GAZ</b>\nOlay #3 · 30.10.2026 18:52:07\n")
    assert "Kaynak: F1 · fiziksel düğüm\n" in text
    assert "Konum: 40.687512, 39.701234 · rakım 1240 m" in text
    assert '<a href="https://www.google.com/maps/search/?api=1&amp;query=40.687512,39.701234">' in text
    assert "Rota: F1 → F2 → N07 → N03 → MERKEZ (4 hop)" in text
    assert "Ölçüm: gaz 776 mV (taban 324) · 24.0 °C · nem %45" in text
    assert "Bölge: Maçka &lt;Altındere&gt; &amp; Sümela" in text
    assert text.endswith("<i>Demo: OGM / İtfaiye / Muhtarlık bildirimleri simülasyondur.</i>")


def test_sanal_simule_yukselme_ve_kapanis_bicimi():
    virtual = {"id": 4, "node": "N07", "node_type": "virtual", "simulated": True, "alarm": "yangın",
               "rakim_m": 900, "lat": 40.7, "lon": 39.65}
    text = format_incident(virtual, "escalated")
    assert text.startswith("<b>ALARM YÜKSELDİ: N07 – YANGIN</b>")
    assert "Kaynak: N07 · sanal düğüm · SİMÜLE" in text
    assert "rakım 900 m" in text and "Ölçüm" not in text and "Rota" not in text

    closed = format_incident(INCIDENT, "closed")
    assert closed.startswith("<b>OLAY KAPATILDI: F1 – GAZ</b>")
    assert "Konum" not in closed and "Ölçüm" not in closed


def test_yardimcilar():
    assert alarm_name(3) == "YANGIN" and alarm_name("gas") == "GAZ" and alarm_name("ısı") == "ISI"
    assert alarm_name(None) == "ALARM"
    moment = datetime(2026, 10, 30, 15, 52, 7, tzinfo=timezone.utc)
    for value in (moment, moment.timestamp(), moment.timestamp() * 1000, "2026-10-30T15:52:07Z",
                  "2026-10-30T18:52:07"):                 # saat dilimsiz metin Türkiye saati sayılır
        assert format_time(value) == "30.10.2026 18:52:07", value


# ------------------------------------------------------------------ gönderim

def test_iki_sohbete_mesaj_ve_konum_ignesi():
    fake = FakeTelegram()
    results = []
    notifier = make(fake, chats=f"{GROUP},@orman_yangin_demo", on_result=results.append)

    async def scenario():
        assert notifier.notify(INCIDENT, "created") is True
        assert notifier.notify(INCIDENT, "created") is False    # aynı olay ikinci kez gitmez
        await notifier.drain()

    asyncio.run(scenario())
    assert fake.methods() == ["sendMessage", "sendMessage", "sendLocation", "sendLocation"]
    message = fake.calls[0][1]
    assert message["chat_id"] == GROUP and message["parse_mode"] == "HTML"
    assert message["link_preview_options"] == {"is_disabled": True}
    assert f"/bot{TOKEN}/sendMessage" in fake.calls[0][2]
    assert fake.calls[1][1]["chat_id"] == "@orman_yangin_demo"
    pin = fake.calls[2][1]
    assert (pin["latitude"], pin["longitude"]) == (40.687512, 39.701234)
    assert pin["disable_notification"] is True and pin["reply_parameters"]["message_id"] == 101

    (result,) = results
    assert result.status == "ok" and (result.sent, result.total) == (2, 2)
    assert result.ui_text.startswith("gönderildi · ")
    event = result.to_event()
    assert event["type"] == "telegram" and event["incident_id"] == 3 and event["text"] == result.ui_text
    json.dumps(event)                                           # WebSocket'e yazılabilir


def test_yukselme_ve_kapanista_igne_yok():
    fake = FakeTelegram()
    notifier = make(fake)
    asyncio.run(notifier.send_incident(INCIDENT, "escalated"))
    asyncio.run(notifier.send_incident(INCIDENT, "closed"))
    assert fake.methods() == ["sendMessage", "sendMessage"]


def test_kismi_hata_ve_token_sizmaz(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeTelegram({"sendMessage": [error(400, "Bad Request: chat not found")]})
    notifier = make(fake, chats=f"-1009999999999,{GROUP}")
    result = asyncio.run(notifier.send_incident(INCIDENT))
    assert result.status == "partial" and (result.sent, result.total) == (1, 2)
    assert result.ui_text.startswith("kısmen gönderildi (1/2) · -1009999999999: Sohbet bulunamadı")
    assert TOKEN not in result.ui_text and TOKEN not in caplog.text


def test_internet_yoksa_bir_kez_dener_sonra_hata(caplog):
    caplog.set_level(logging.DEBUG)
    down = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
    fake = FakeTelegram({"sendMessage": [down, down]})
    result = asyncio.run(make(fake).send_incident(INCIDENT))
    assert result.status == "error" and "bağlanılamadı" in result.ui_text
    assert fake.methods() == ["sendMessage", "sendMessage"]
    assert TOKEN not in result.ui_text and TOKEN not in caplog.text


def test_gecersiz_token():
    fake = FakeTelegram({"sendMessage": [error(401, "Unauthorized")]})
    result = asyncio.run(make(fake).send_incident(INCIDENT))
    assert result.status == "error" and "Token geçersiz" in result.detail


def test_supergrup_gocunde_yeni_kimligi_izler():
    new_id = -1005555555555
    fake = FakeTelegram({"sendMessage": [error(400, "Bad Request: group chat was upgraded to a supergroup chat",
                                               migrate_to_chat_id=new_id)]})
    notifier = make(fake, chats="-4123456789")
    result = asyncio.run(notifier.send_incident(INCIDENT))
    assert result.status == "ok" and str(new_id) in result.detail
    assert [call[1]["chat_id"] for call in fake.calls] == [-4123456789, new_id, new_id]
    assert fake.methods()[-1] == "sendLocation" and notifier.chat_ids == [new_id]


def test_hiz_sinirinda_bekleyip_tekrar_dener():
    fake = FakeTelegram({"sendMessage": [error(429, "Too Many Requests: retry after 2", retry_after=2)]})
    result = asyncio.run(make(fake, send_pin=False).send_incident(INCIDENT))
    assert result.status == "ok" and fake.methods() == ["sendMessage", "sendMessage"]


def test_igne_gitmezse_mesaj_yine_basarili():
    fake = FakeTelegram({"sendLocation": [error(400, "Bad Request: not enough rights to send locations")]})
    result = asyncio.run(make(fake).send_incident(INCIDENT))
    assert result.status == "ok" and "konum iğnesi gitmedi" in result.detail


def test_sifirla_sonrasi_ayni_olay_tekrar_gider():
    fake = FakeTelegram()
    notifier = make(fake, send_pin=False)

    async def scenario():
        assert notifier.notify(INCIDENT)
        await notifier.drain()
        assert not notifier.notify(INCIDENT)
        notifier.reset()
        assert notifier.notify(INCIDENT)
        await notifier.drain()

    asyncio.run(scenario())
    assert fake.methods() == ["sendMessage", "sendMessage"]


def test_threadden_notify_async_geri_cagri_ve_hatali_geri_cagri():
    fake = FakeTelegram()
    statuses = []

    async def scenario():
        async def on_result(result):
            statuses.append(result.status)
            raise RuntimeError("arayüz yayını patladı")         # olay akışını bozmamalı

        notifier = TelegramNotifier(TOKEN, str(GROUP), send_pin=False, on_result=on_result,
                                    transport=httpx.MockTransport(fake.handler))
        assert await asyncio.to_thread(notifier.notify, INCIDENT) is True
        await asyncio.sleep(0)
        await notifier.drain()
        await notifier.aclose()

    asyncio.run(scenario())
    assert statuses == ["ok"] and fake.methods() == ["sendMessage"]


def test_kapaliyken_ag_cagrisi_yapilmaz():
    fake = FakeTelegram()
    notifier = TelegramNotifier("", "", transport=httpx.MockTransport(fake.handler))
    result = asyncio.run(notifier.send_incident(INCIDENT))
    assert result.status == "error" and fake.calls == []


def test_httpx_loglari_kapali():
    assert logging.getLogger("httpx").level >= logging.WARNING
