# CLAUDE.md — Proje kuralları (her oturumda geçerli)

Orman yangını erken uyarı mesh ağı · hackathon jüri demosu · Hardware-in-the-Loop.
F1 (ESP32-S, pilli saha düğümü) → nRF24 → F2 (DevKit V1, USB köprü) → bilgisayar → gerçek DEM üzerinde
3B harita + sanal LoRa mesh → MERKEZ → OGM / İtfaiye / Muhtarlık (simüle bildirim).

## Doğruluk kaynakları
- Seri/radyo protokolü: `docs/PROTOCOL.md`. Firmware ile birlikte değişir; tek taraflı değiştirme.
- Donanım ve güç: `docs/HARDWARE.md`. Gerçek oturum örneği: `docs/ornek_seri_kayit.tsv`.
- Firmware (`firmware/`) test edildi. Protokol değişikliği gerekiyorsa: iki main.cpp + PROTOCOL.md + testler birlikte,
  ve önce kullanıcıya sor.

## Komutlar
- Çalıştır: `python3 simulation/run.py` (Windows: `py simulation\run.py`) · demo: `--source demo` · replay: `--source replay`
- Testler: `simulation/.venv/bin/python -m pytest simulation/tests` · firmware mantığı: `bash firmware/test_host/run.sh`
- Çevrimdışı harita önbelleği: `python simulation/tools/prefetch_tiles.py`
- Seri port testi: `python firmware/tools/seri_test.py`

## Arayüz kuralları (AI slop yasağı)
- Tek ekran, sekme/modal yok. Üst çubuk + harita + sağ panel (Fiziksel Düğümler · Olay · Kayıt). Yeni panel/sürgü ekleme.
- Renk yalnızca durum: normal #C9D1D9 · uyarı #F5A524 · alarm #E5484D · bağlantı yok #5B6470 · rota/seçim #4C9AFF.
  Zemin #0E1116, panel #151A21, çizgi #262D36, metin #E6E9EE / #9AA4B2. Başka renk yok.
- Emoji, gradyan, gölge, blur, neon, glow yok. Köşe ≤ 6 px. Yazı ≥ 12 px. Sistem fontları; sayılar tabular-nums.
- Animasyon yalnızca: paket noktası, alarm halkası (1 Hz), yeni olay vurgusu (≤ 600 ms). prefers-reduced-motion.
- Uydurma metrik yok. Her sayı ya ölçülmüş (F1/F2) ya da modelden hesaplanmış ve "simüle" etiketli.
- Türkçe, kısa, somut metin. Pazarlama dili yok.

## Teknik kurallar
- Çalışma anında CDN yok (MapLibre 5.24.0 `simulation/web/vendor/` altında). Demo çevrimdışı çalışabilmeli.
- Seri port: `dtr=False, rts=False`; JSON olmayan satırı yok say; `(node, up, seq)` ile tekrarları ele; Linux + Windows.
- Gerçek kurumlarla asla iletişim kurma. Telegram yalnızca `.env` ile, ekibin kendi sohbetine.
- Sır (token, .env) ve harita karosu önbelleği (`simulation/data/`) commit edilmez.
