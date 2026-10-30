#!/usr/bin/env python3
"""Telegram bildirim testi — demo öncesi çalıştırın. Token ekrana hiçbir zaman yazdırılmaz.

  Linux:   simulation/.venv/bin/python simulation/tools/telegram_test.py [--sohbetler | --ornek]
  Windows: simulation\\.venv\\Scripts\\python simulation\\tools\\telegram_test.py [--sohbetler | --ornek]

  (seçeneksiz)  token'ı doğrular ve .env'deki her sohbete kısa bir test mesajı yollar
  --sohbetler   botun gördüğü sohbetleri ve kimliklerini listeler (TELEGRAM_CHAT_ID'yi bulmak için)
  --ornek       demodaki olay mesajının aynısını (konum iğnesiyle) yollar

Ayarlar simulation/.env dosyasından okunur (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_SEND_PIN).
Çıkış kodu: 0 başarılı · 1 Telegram hatası · 2 ayar eksik/hatalı.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parents[1]
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

try:
    from server.telegram_notify import TelegramNotifier
except ModuleNotFoundError as exc:
    if exc.name == "httpx":
        sys.exit("httpx bulunamadı: projenin venv'iyle çalıştırın (simulation/.venv/bin/python …)")
    raise


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sample_incident() -> dict:
    """Örnek olay: konum config/nodes.json'daki F1'den, bölge adı config/area.json'dan (varsa).
    Ölçüm değerleri docs/ornek_seri_kayit.tsv'deki gerçek olay paketinden."""
    incident = {"id": "TEST", "node": "F1", "node_type": "physical", "simulated": True, "alarm": 1,
                "lat": 40.700000, "lon": 39.660000,
                "reading": {"gas": 776, "base": 324, "t": 24.0, "h": 45.0}}
    nodes = _load_json(SIM_DIR / "config" / "nodes.json")
    if isinstance(nodes, dict):
        nodes = nodes.get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if isinstance(node, dict) and node.get("id") == "F1":
            for key in ("lat", "lon", "rakim_m"):
                if node.get(key) is not None:
                    incident[key] = node[key]
            break
    area = _load_json(SIM_DIR / "config" / "area.json")
    if isinstance(area, dict):
        name = next((area[key] for key in ("title", "baslik", "ad", "name") if isinstance(area.get(key), str)), None)
        if name:
            incident["area"] = name
    return incident


async def list_chats(notifier: TelegramNotifier) -> int:
    me = await notifier.check_token()
    if not me.ok:
        print(f"Token sorunu: {me.error}")
        return 1
    bot = (me.result or {}).get("username", "?")
    print(f"Bot: @{bot}")
    reply, chats = await notifier.list_recent_chats()
    if not reply.ok:
        print(f"Güncellemeler alınamadı: {reply.error}")
        return 1
    if not chats:
        print("Henüz sohbet görünmüyor:")
        print(f"  Grup:    botu gruba ekleyin, grupta /start@{bot} yazın.")
        print("  Kanal:   botu yönetici yapın. Herkese açık kanalda kimlik gerekmez: TELEGRAM_CHAT_ID=@kanal_adi")
        print("  Kişisel: bota özelden /start gönderin.")
        print("Sonra bu komutu tekrar çalıştırın (Telegram son 24 saati tutar).")
        return 1
    print("Görülen sohbetler:")
    for chat in chats:
        name = chat["title"] + (f" (@{chat['username']})" if chat.get("username") else "")
        print(f"  {chat['id']:>16}  {chat['type']:<10}  {name}")
    first = chats[0]
    suggestion = f"@{first['username']}" if first["type"] == "channel" and first.get("username") else first["id"]
    print(f"\n.env için örnek:  TELEGRAM_CHAT_ID={suggestion}")
    print("Birden fazla sohbet için virgülle ayırın.")
    return 0


async def send_test(notifier: TelegramNotifier) -> int:
    if not notifier.enabled:
        print(notifier.describe())
        return 2
    me = await notifier.check_token()
    if not me.ok:
        print(f"Token sorunu: {me.error}")
        return 1
    print(f"Bot: @{(me.result or {}).get('username', '?')} · {len(notifier.chat_ids)} sohbet")
    failed = 0
    for chat, reply in await notifier.send_test_message():
        print(f"  {chat}: {'gönderildi' if reply.ok else 'HATA — ' + reply.error}")
        if reply.note:
            print(f"    not: {reply.note}")
        failed += not reply.ok
    return 1 if failed else 0


async def send_sample(notifier: TelegramNotifier) -> int:
    if not notifier.enabled:
        print(notifier.describe())
        return 2
    result = await notifier.send_incident(sample_incident(), "created")
    print(f"Örnek olay mesajı: {result.ui_text}")
    return 0 if result.ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Telegram bildirim testi (ayarlar: simulation/.env)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sohbetler", action="store_true",
                       help="botun gördüğü sohbetleri ve kimliklerini listele")
    group.add_argument("--ornek", action="store_true",
                       help="demodaki olay mesajının aynısını konum iğnesiyle gönder")
    args = parser.parse_args(argv)

    logging.getLogger("telegram").setLevel(logging.ERROR)   # araç sonuçları kendisi yazdırır
    notifier = TelegramNotifier.from_env([SIM_DIR / ".env", SIM_DIR.parent / ".env"])
    if args.sohbetler:
        if not notifier.token_ok:
            print("TELEGRAM_BOT_TOKEN boş ya da biçimi hatalı (simulation/.env).")
            return 2
        return asyncio.run(list_chats(notifier))
    return asyncio.run(send_sample(notifier) if args.ornek else send_test(notifier))


if __name__ == "__main__":
    sys.exit(main())
