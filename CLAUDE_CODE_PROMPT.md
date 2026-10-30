# Claude Code İnşa Promptu

**Kullanım:** Bu kiti bir klasöre açın, o klasörde terminalde `claude` çalıştırın ve aşağıdaki kod bloğunun
**tamamını** yapıştırın. (Alternatif: Claude Code'a yalnızca *"CLAUDE_CODE_PROMPT.md dosyasındaki görevi uygula"* yazın.)

````text
# GÖREV
"Orman Yangını Erken Uyarı ve Haberleşme Mesh Ağı (Hardware-in-the-Loop)" projesinin simülasyonunu,
sunucusunu, testlerini ve dokümantasyonunu bu klasörde SIFIRDAN inşa et; sonra GitHub'a gönder.
Bu bir hackathon jüri demosu: önce GÜVENİLİR ve SADE çalışması, sonra güzel görünmesi gerekiyor.
Sen kıdemli bir full-stack + CBS (GIS) mühendisi gibi çalış: oku → planla → inşa et → test et → belgele → gönder.

====================================================================================================
## 0. BAŞLAMADAN ÖNCE (kod yazmadan)
====================================================================================================
1. Bu klasörde aşağıdakiler ZATEN VAR, test edildi ve doğrulandı. İÇERİKLERİNİ DEĞİŞTİRME:
   - firmware/node1_sensor/   (F1 · ESP32-S · PlatformIO)       - firmware/node2_gateway/  (F2 · DevKit V1)
   - firmware/test_host/      (firmware mantık testleri, g++)   - firmware/tools/seri_test.py
   - docs/HARDWARE.md · docs/PROTOCOL.md · docs/ornek_seri_kayit.tsv · docs/wiring-node1.svg · docs/wiring-node2.svg
   - docs/kablolama.html (etkileşimli bağlantı şeması, çevrimdışı açılır)
   - CLAUDE.md (kalıcı proje kuralları — her oturumda uy)
   Biri eksikse DUR ve bana söyle. Firmware'de protokol hatası bulursan düzeltmeden önce bana sor.
2. docs/PROTOCOL.md'yi baştan sona oku: seri port verisinin TEK doğruluk kaynağı odur.
   docs/ornek_seri_kayit.tsv gerçek firmware'in ürettiği bir oturumdur (biçim: "<ms>\t<json satırı>").
   Ayrıştırıcı testlerinde ve replay/demo modunda bunu kullan.
3. Ortamı kontrol et ve bana tek satırda raporla (eksikse durma): python3 --version (≥3.10), git, gh, pio, g++.
   `bash firmware/test_host/run.sh` çalıştır (g++ varsa) ve sonucunu not et.
4. Kısa bir uygulama planı yaz (dosya listesi + sıra), sonra onay beklemeden uygula.
   Öncelik sırası: FAZ 1 bitmeden FAZ 2'ye geçme.
   - FAZ 1 (ZORUNLU): uçtan uca demo yolu — sunucu + seri köprü + demo modu + harita (gerçek DEM) + mesh
     animasyonu + olay/kurum paneli + testler + README + git.
   - FAZ 2 (zaman kalırsa): görüş hattı (LoS) ile link hesabı, çoklu port, Telegram bildirimi, harita katman seçimi.

====================================================================================================
## 1. DEMO SENARYOSU = KABUL KRİTERİ
====================================================================================================
Masada 2 fiziksel düğüm var; simülasyonda haritanın EN UÇ noktasında F1 ve F2 olarak görünürler:
1. Jüri F1'in MQ sensörüne çakmak gazı tutar. F1 buzzer'ı anında öter ve nRF24 ile F2'ye paket yollar.
2. F2 (USB'ye takılı) satırı anında bilgisayara basar: {"type":"tel","node":1,"via":"rf","kind":"event","alarm":1,...}
3. Satır geldikten sonra ≤ 150 ms içinde haritada F1 KIRMIZI olur (alarm metniyle birlikte).
4. Hemen ardından paket animasyonu başlar: F1 → F2 (etiket: "gerçek RF · nRF24 2.4 GHz") → sanal mesh
   düğümleri (etiket: "simüle LoRa 868 MHz") → MERKEZ. Rota üzerindeki her hop sırayla vurgulanır.
5. Paket MERKEZ'e ulaşınca bir OLAY oluşur ve OGM, İtfaiye, Muhtarlık satırları sırayla "Gönderiliyor…" →
   "İletildi (simülasyon)" olur (zaman damgalarıyla).
6. "Sahadaki sireni sustur" butonu köprüye `silence 1` yollar; F1'den flags&4 gelince arayüz
   "F1 onayladı: siren susturuldu" yazar (çift yönlü bağlantının kanıtı).
7. Donanım takılı değilken üst çubuktaki "Simüle Çakmak Tetikle" butonu AYNI hattı (aynı ayrıştırıcı, aynı olay
   akışı) kullanarak 1–6'yı birebir oynatır; kayıtta "SİMÜLE" etiketiyle görünür.
8. F1'den 3.5 s veri gelmezse ({"type":"link","state":"lost"}) F1 gri/kesik çizgili "BAĞLANTI YOK" olur.

====================================================================================================
## 2. SABİT TEKNOLOJİ KARARLARI (değiştirme)
====================================================================================================
- Backend: Python ≥3.10 · FastAPI · uvicorn · pyserial · httpx · Pillow · pydantic v2 · python-dotenv · pytest.
- Frontend: DERLEME ADIMI YOK. Düz HTML + CSS + vanilla JS (ES modules). Node/npm gerektirme.
- Harita: MapLibre GL JS **5.24.0** (UMD sürümü; v6 ESM-only olduğu için kullanma). Dağıtım dosyalarını
  (`dist/maplibre-gl.js`, `dist/maplibre-gl.css`, `dist/LICENSE.txt`) `https://registry.npmjs.org/maplibre-gl/-/maplibre-gl-5.24.0.tgz`
  paketinden Python ile indirip `simulation/web/vendor/maplibre-gl/` altına koy ve REPOYA COMMIT ET
  (`simulation/tools/vendor_maplibre.py`). Çalışma anında hiçbir CDN'e (unpkg, jsdelivr, tailwind, Google Fonts) bağlanma.
- Tek komut: Linux `python3 simulation/run.py` · Windows `py simulation\run.py`. run.py: `.venv` yoksa oluşturur,
  requirements kurar, kendini venv içinde yeniden başlatır, sunucuyu açar, tarayıcıyı açar. Ayrıca `run.sh` ve `run.bat`
  sarmalayıcıları. Bayraklar: `--source auto|serial|demo|replay`, `--port`, `--baud 115200`, `--http-port 8000`,
  `--host 127.0.0.1`, `--no-browser`, `--replay-file`.

====================================================================================================
## 3. KLASÖR YAPISI (hedef)
====================================================================================================
```
.
├── README.md  LICENSE(MIT)  .gitignore  .gitattributes  CLAUDE.md  CLAUDE_CODE_PROMPT.md
├── firmware/                     (mevcut — dokunma)
├── docs/                         (mevcut + ARCHITECTURE.md ve ekran görüntüsü yer tutucusu ekle)
└── simulation/
    ├── run.py  run.sh  run.bat  requirements.txt  requirements-dev.txt  .env.example
    ├── config/  area.json  nodes.json (üretilir, commit edilir)  agencies.json
    ├── server/  __init__.py  main.py  config.py  protocol.py  serial_bridge.py  demo.py  terrain.py
    │            mesh.py  incidents.py  notify.py  tiles.py  state.py
    ├── web/     index.html  style.css  js/{app,map,ui,ws}.js  vendor/maplibre-gl/
    ├── tools/   vendor_maplibre.py  prefetch_tiles.py  generate_layout.py
    ├── data/    (gitignore: tiles/ önbellek, logs/ oturum kayıtları)
    └── tests/   test_protocol.py  test_mesh.py  test_terrain.py  test_incidents.py  test_smoke.py
```

====================================================================================================
## 4. GERÇEK 3B ARAZİ (yapay zekâ üretimi model YASAK)
====================================================================================================
- Bölge (varsayılan, `config/area.json`): Trabzon / Maçka – Altındere Vadisi ormanı (Sümela çevresi),
  bbox: güney 40.660, batı 39.600, kuzey 40.740, doğu 39.720. Başlık, bbox, kamera (merkez, zoom 12.8, pitch 60,
  bearing) ve mesh parametreleri bu dosyada. Başka bölge = yeni JSON, kod değişmez (`--area`).
- Yükseklik: AWS Open Data Terrain Tiles (Terrarium kodlaması, z ≤ 15, anahtar gerekmez):
  `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`
  Çözme: yükseklik_m = (R × 256 + G + B / 256) − 32768. MapLibre'de `raster-dem`, `encoding: "terrarium"`,
  `tileSize: 256`, `maxzoom: 15`; `setTerrain({ exaggeration: 1.3 })`; hafif `hillshade` katmanı; `sky`.
- Görüntü: Esri World Imagery (`https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}`,
  DİKKAT y/x sırası) — `raster-saturation: -0.3` ile hafif soluk. FAZ 2: ikinci seçenek OpenTopoMap
  (`https://tile.opentopomap.org/{z}/{x}/{y}.png`). Atıflar haritada görünür olmalı (Esri, AWS Terrain Tiles/Mapzen, OSM/OpenTopoMap).
- Tüm karolar backend proxy'sinden geçer: `GET /tiles/{layer}/{z}/{x}/{y}` → önce `simulation/data/tiles/` diskte,
  yoksa üst kaynaktan (httpx, 8 s zaman aşımı, User-Agent "orman-yangin-mesh/1.0 (hackathon demo)") indir, kaydet, sun.
  Ulaşılamazsa 404 dön ve arayüzün durum satırında "Harita karosu çevrimdışı" göster.
- ÇEVRİMDIŞI DEMO ŞART (salon Wi-Fi'ına güvenme): `python simulation/tools/prefetch_tiles.py` bbox (+%10 pay) için
  DEM z9–15 ve uydu z9–16 karolarını önbelleğe alır, sayıyı ve MB'ı yazdırır. Haritaya `maxBounds` (bbox + pay),
  `minZoom 11`, `maxZoom 16` koy ki demoda önbellek dışına çıkılmasın. README'de "demo öncesi prefetch çalıştırın".
- Karolar üçüncü taraf lisanslıdır: önbelleği (data/tiles) asla commit etme.

====================================================================================================
## 5. DÜĞÜMLER VE GERÇEK KOORDİNATLAR
====================================================================================================
- `simulation/tools/generate_layout.py` (deterministik, sabit seed) gerçek DEM'den `config/nodes.json` üretir;
  sonuç commit edilir, sunucu bu dosyayı okur (yoksa üretir).
  * F1 (fiziksel saha düğümü) ve F2 (fiziksel köprü): bbox'ın güneydoğu ucunda, ormanın en uzak noktası.
    F1 en uçta; F2 ondan 300–500 m içeride. İkisi de yerel sırt/yüksek noktaya oturur.
  * MERKEZ (yangın gözetleme/komuta, internet çıkışı): karşı uçta (kuzeybatı), yerel en yüksek noktalardan biri.
  * Sanal düğümler (varsayılan 16, config'ten): aralarında ≥ 600 m, sırt tercihli (yerel yükseklik maksimumu),
    deniz/vadi tabanı (< 50 m veya çevresine göre çukur) hariç. Graf bağlı olmalı; her sanal düğümün ≥ 2 komşusu
    olmalı (yedeklilik); F1 → MERKEZ en kısa yolu 5–9 hop olmalı. Kısıt sağlanmazsa aralığı gevşet, yeniden dene.
  * Her kayıt: id (F1, F2, N01…N16, MERKEZ), tür (physical | virtual | center), ad, lat, lon (6 ondalık),
    rakim_m (DEM'den), direk_m (4), donanım ("ESP32-S + nRF24 (prototip)" / "LoRa SX1262 (konsept)").
- Koordinatlar arayüzde düğüm açılır kutusunda ve README'deki üretilmiş tabloda görünür.

====================================================================================================
## 6. MESH MODELİ (açıklanabilir olmalı — jüri soracak)
====================================================================================================
- Link kuralı (FAZ 1): mesafe ≤ `range_m` (varsayılan 2500) → link var. FAZ 2: ayrıca DEM profilinde görüş hattı
  kontrolü (direk 4 m, 868 MHz'te 1. Fresnel bölgesinin %60'ı açık, dünya eğriliği k=4/3); engelliyse ve mesafe
  > 800 m ise link yok. Link maliyeti = mesafe_km (+ zayıf linklere ceza). Hepsi `mesh.py` içinde, birim testli.
- Rota: Dijkstra (kaynak → MERKEZ). Bir sanal düğüme tıklayıp "Devre dışı bırak" denince rota yeniden hesaplanır ve
  log'a "Rota yeniden hesaplandı: F1 → … → MERKEZ (n hop)" yazılır.
- Gecikme modeli: LoRa hava süresi (Semtech AN1200.13) — SF9, BW 125 kHz, CR 4/5, preamble 8, CRC açık, yük 24 B →
  ≈ 206 ms/hop (+20 ms işlem). Formül `mesh.py`'de, testte SF9/125k/24B için 205.8 ± 1 ms doğrulanır.
- Animasyon: her hop 600 ms (config), kullanıcı görsün diye. Olay panelinde ayrıca "Hesaplanan ağ gecikmesi:
  X ms (n hop × ~206 ms, SF9/125 kHz) — simülasyon" yaz. Animasyon süresini gecikme diye GÖSTERME.
- F1 → F2 hop'u gerçektir; diğerleri simülasyondur. Arayüz ve README bu ayrımı açıkça söyler. Prototipte LoRa yerine
  nRF24L01+ kullanıldığı, protokol/yönlendirme mantığının radyodan bağımsız olduğu README'de yazar.

====================================================================================================
## 7. SERİ KÖPRÜ (Linux + Windows, tek kod)
====================================================================================================
- Port bulma: `serial.tools.list_ports`; önce bilinen USB-UART VID:PID'ler (CP210x 10C4:EA60, CH340 1A86:7523,
  CH9102 1A86:55D4, FTDI 0403:6001), sonra adlar (/dev/ttyUSB*, /dev/ttyACM*, COM*). Portu `dtr=False, rts=False`
  ile aç (ESP32 resetlenmesin), `status\n` gönder, 2.5 s içinde `hello`/`tel` gelirse kabul et; `hello.role` ile
  köprüyü (gateway) ve doğrudan takılı F1'i (sensor, B planı) ayırt et. FAZ 2: birden çok portu aynı anda oku.
- Okuma ayrı bir thread'de; satırlar thread-güvenli şekilde asyncio kuyruğuna aktarılır. `{` ile başlamayan veya
  çözülemeyen satırları yok say. `(node, up, seq)` ile tekrarları ele. Her satırı alındığı anda işle (toplama yok).
- Kopma/çıkarma: 2 s'de bir yeniden dene; arayüzde "Donanım bağlantısı koptu — yeniden deneniyor". `--source auto`:
  donanım yoksa DEMO moduna geç ve 3 s'de bir port taramaya devam et; köprü takılınca otomatik donanım moduna geç.
- Anlaşılır hata mesajları: Linux PermissionError → "sudo usermod -aG dialout $USER, sonra oturumu kapatıp açın";
  Ubuntu'da port görünmüyorsa → "sudo apt remove brltty"; Windows'ta port meşgul → "Arduino IDE / seri monitörü kapatın";
  sürücü → CH340/CP210x.
- Komutlar: `POST /api/command {target:1|2|"all", cmd:"silence"|"test"}` → köprüye `silence 1\n` vb.; köprünün
  `{"type":"cmd"}` yanıtı log'a düşer. Demo modunda komutlar sanal F1'e uygulanır (flags&4 üretilir).
- Her ham satır `simulation/data/logs/oturum-YYYYMMDD-HHMMSS.jsonl` dosyasına zaman damgasıyla yazılır.

====================================================================================================
## 8. DEMO VE REPLAY
====================================================================================================
- `demo.py`, firmware'le BİREBİR aynı biçimde JSON satırları üretir (sanal F1 + F2: 1 s periyot, gerçekçi
  t/h/gas/base, ilk 45 s flags=1 ısınma). Bu satırlar gerçek seri satırlarla AYNI ayrıştırıcıdan geçer.
- "Simüle Çakmak Tetikle": sanal F1'de gaz 300 ms içinde base+800 mV'a çıkar → `kind:"event", alarm:1`;
  6 s sonra sıcaklık yükselir → alarm 3; 25 s sonra söner. Donanım bağlıyken de çalışır ve "SİMÜLE" etiketlenir.
- `--source replay --replay-file docs/ornek_seri_kayit.tsv`: dosyadaki satırları kayıttaki zamanlamayla oynatır.

====================================================================================================
## 9. OLAY AKIŞI VE KURUM BİLDİRİMİ (state.py / incidents.py)
====================================================================================================
- Düğüm durumu: NORMAL · GAZ · ISI · YANGIN (alarm 0/1/2/3) · BAĞLANTI YOK (link lost ya da 3.5 s veri yok) ·
  ISINIYOR (flags&1, rozet olarak) · DEVRE DIŞI (kullanıcı kapattı, yalnız sanal).
- Fiziksel düğüm alarm > 0 olunca: yoksa yeni olay aç (no, kaynak, başlangıç, alarm tipi, lat/lon, rakım, rota,
  hop sayısı, hesaplanan gecikme), animasyonu başlat. Animasyon bitince MERKEZ "aldı" → OGM (+0.3 s), İtfaiye
  (+0.6 s), Muhtarlık (+0.9 s): her biri "Gönderiliyor…" → 0.8 s sonra "İletildi (simülasyon)". Zamanlayıcılar
  backend'de; tüm sekmeler aynı durumu görür. Saat enjekte edilebilir olsun (testler gerçek zamanı beklemesin).
- Alarm GAZ → YANGIN yükselirse olay güncellenir ve log'a yazılır; kaynak normale dönünce "Kaynak normale döndü"
  yazılır ama olay operatör "Olayı kapat" diyene kadar açık kalır. "Sıfırla" tüm olayları ve animasyonları temizler.
- Gerçek kurumlarla ASLA iletişim kurma; kurum bildirimleri simülasyondur ve arayüzde öyle yazar.
- FAZ 2 (isteğe bağlı): `.env` içinde TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID varsa olay mesajı ekibin kendi Telegram
  sohbetine gerçekten gönderilsin (başlık, kaynak, koordinat + Google Maps linki, rakım, alarm tipi, saat).
  Sonuç arayüzde "Telegram: gönderildi / hata" olarak görünür. Token yoksa bu özellik sessizce kapalı. `.env.example` ekle.

====================================================================================================
## 10. ARAYÜZ — "AI SLOP" YASAĞI (en önemli bölüm)
====================================================================================================
Bize örnek diye gösterilen bir sayfa şunları yapıyordu ve hepsi YASAK:
3 sekme, 6 kart, 10+ sürgü (rüzgâr, güneş saati, düğüm sayısı, menzil, yerleşim algoritması…); her yerde emoji;
gradyan butonlar, cam efekti (backdrop-blur), neon mor/fuşya/camgöbeği/zümrüt aynı anda; sürekli yanıp sönen
ping/pulse/bounce animasyonları; 8–10 px yazılar; uydurma metrikler ("%98.2 kapsama", "AI anomali skoru 0.08",
"Isolation Forest", "5+ yıl otonom", "15 km+ menzil"); gerçek donanımla çelişen kod (DHT22/LoRa) ve
sentetik 3B çam ağaçları. Bizimki bunun tersi olacak:

DÜZEN (tek ekran, sekme yok, modal yok, 1366×768'de kaydırma yok):
- Üst çubuk (48 px): solda "Orman Yangını Erken Uyarı Ağı" + bölge adı · ortada sistem durumu metni
  (NORMAL / ALARM: F1 – GAZ) · sağda veri kaynağı ("Donanım: /dev/ttyUSB0" ya da "DEMO"), saat ve TAM İKİ buton:
  [Simüle Çakmak Tetikle] [Sıfırla].
- Sol: harita (kalan genişliğin tamamı).
- Sağ panel (360 px), yukarıdan aşağı TAM ÜÇ bölüm:
  1) "Fiziksel Düğümler": F1 ve F2 için birer satır kart — durum, sıcaklık, nem, gaz (mV ve taban farkı),
     son veri ("0.4 s önce"), bağlantı kalitesi (lq). F1 kartında son 60 s'nin gaz değerini gösteren küçük bir
     çizgi (sparkline, <canvas>, kütüphanesiz). Başka grafik yok.
  2) "Olay": olay yoksa "Aktif olay yok". Varsa: olay no, saat, kaynak, koordinat, rakım, alarm tipi, rota
     ("F1 → F2 → N07 → … → MERKEZ, 6 hop"), hesaplanan gecikme; altında OGM / İtfaiye / Muhtarlık satırları
     (durum + saat); butonlar [Sahadaki sireni sustur] [Olayı kapat].
  3) "Kayıt": zaman damgalı düz metin akışı (monospace, en yeni üstte, en fazla 200 satır).
- Düğüme tıklayınca küçük açılır kutu: ad, tür, koordinat, rakım, komşular; sanal düğümde [Devre dışı bırak]
  ve [Burada yangın simüle et]. Başka kontrol YOK.

GÖRSEL KURALLAR:
- Renk YALNIZCA durum anlatır. Palet (CSS değişkeni olarak tanımla, başka renk ekleme):
  zemin #0E1116 · panel #151A21 · çizgi #262D36 · metin #E6E9EE · ikincil metin #9AA4B2 ·
  normal düğüm #C9D1D9 · uyarı (GAZ/ISI) #F5A524 · alarm (YANGIN ve tüm aktif alarm vurgusu) #E5484D ·
  bağlantı yok #5B6470 · rota/paket/seçim #4C9AFF.
- Durum asla yalnız renkle verilmez: her zaman metin de yazar (erişilebilirlik, projeksiyon).
- Yazı: sistem fontları (ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif); sayılar monospace +
  `font-variant-numeric: tabular-nums`. En küçük yazı 12 px, gövde 14 px. Web fontu indirme.
- Gradyan, gölge, blur, cam efekti, neon, glow, emoji YOK. Köşe yarıçapı ≤ 6 px. 1 px çizgiler. Opak paneller.
- Animasyon yalnızca: (a) rota üzerinde ilerleyen paket noktası, (b) alarmdaki düğümde 1 Hz tek halka,
  (c) yeni olayda panelin bir kez ≤ 600 ms vurgulanması. `prefers-reduced-motion` desteklenir.
- Haritada sanal düğümler daire, fiziksel düğümler KARE (F1/F2 etiketli), MERKEZ daha büyük kare. Linkler ince
  (1.5 px, beyaz %35); aktif rota 4 px #4C9AFF. Lejant: sol alt, en fazla 5 madde.
- Düğümleri ve etiketlerini HTML `maplibregl.Marker` ile çiz (symbol katmanı glyph sunucusu ister, çevrimdışı çalışmaz).
- Varsayılan kamera F1'den MERKEZ'e uzanan tüm ağı çerçeveler; [Görünümü sıfırla] harita köşesinde küçük bir buton.
- Metinler Türkçe, kısa ve somut. "Yapay zekâ", "akıllı", "devrim niteliğinde" gibi pazarlama kelimeleri yok.
  Gösterilen her sayı ya ölçülmüştür (F1/F2) ya da belgelenmiş modelden hesaplanmıştır ve "simüle" etiketlidir.
- 1920×1080 projeksiyonda ve 1366×768 dizüstünde okunur olmalı.

====================================================================================================
## 11. API (öneri — tutarlı kalsın)
====================================================================================================
GET / · GET /api/config (bölge, düğümler, linkler, kurumlar) · GET /api/state · WS /ws ·
POST /api/demo/trigger · POST /api/reset · POST /api/command · POST /api/node/{id}/toggle ·
POST /api/node/{id}/simulate · GET /tiles/{layer}/{z}/{x}/{y} · GET /api/health.
WS olayları: snapshot, telemetry, node_state, link, source_status, propagation {id, path, hop_ms, started_at},
incident_created, incident_received, dispatch {agency, status, at}, incident_updated, log.

====================================================================================================
## 12. TEST VE DOĞRULAMA (bitirmeden önce hepsi yeşil)
====================================================================================================
- pytest: (1) docs/ornek_seri_kayit.tsv'nin tüm satırları ayrıştırılır; kayıttaki zamanlara göre F1 durumu
  62.3 s'de GAZ, 105.3 s'de BAĞLANTI YOK, 108.1 s'de geri gelir; (2) Terrarium çözme (bilinen RGB → yükseklik);
  (3) LoRa hava süresi 205.8 ± 1 ms; (4) Dijkstra + düğüm kapatınca yeniden rota; (5) LoS (FAZ 2: sentetik tepeyle
  engellenen link); (6) olay akışı enjekte saatle: alarm → olay → 3 kurum "İletildi"; (7) tekrar satırlarının elenmesi.
- Duman testi: sunucuyu demo modunda rastgele portta başlat, WS'ye bağlan, /api/demo/trigger çağır;
  15 s içinde incident_received ve 3 dispatch olayını doğrula.
- `bash firmware/test_host/run.sh` (g++ varsa) yine geçmeli.
- Frontend'i kendin göremiyorsan bana elle kontrol listesi ver (yükleme, F1 kırmızı, animasyon, 3 kurum, sustur,
  sıfırla, 1366×768'de kaydırma yok, internet kapalıyken prefetch sonrası harita).

====================================================================================================
## 13. README.md (Türkçe, sade, eksiksiz)
====================================================================================================
Bölümler: Proje özeti (2 paragraf: sorun + çözüm) · Ekran görüntüsü yer tutucusu (docs/ekran.png) ·
Mimari (Mermaid: F1 → nRF24 → F2 → USB → sunucu → WebSocket → tarayıcı; konseptte LoRa mesh → Merkez → kurumlar) ·
Donanım (docs/HARDWARE.md'ye link + kısa pin tablosu + docs/wiring-node1.svg görseli + "4×AA → VIN, F2 USB") ·
Hızlı başlangıç **Linux (Ubuntu)** ve **Windows** AYRI AYRI (Python kurulumu, dialout/brltty, CH340/CP210x sürücüleri,
tek komut) · Firmware yükleme (PlatformIO ve Arduino IDE) · Çalıştırma modları (donanım / demo / replay) ·
Demo öncesi kontrol listesi (prefetch, pil, 3 dk MQ ısınması) · Jüri demosu adım adım · Sorun giderme (en sık 8) ·
Proje yapısı · Teknik notlar (LoRa vs nRF24 dürüstlüğü, mesh modeli varsayımları, gecikme modeli, veri kaynakları ve
atıflar) · Düğüm koordinatları tablosu (nodes.json'dan üretilmiş) · Lisans · Ekip (yer tutucu).

====================================================================================================
## 14. GIT VE GITHUB
====================================================================================================
1. `.gitignore`: .venv/, __pycache__/, *.pyc, .pytest_cache/, .pio/, .vscode/ (settings hariç), simulation/data/tiles/,
   simulation/data/logs/, .env, .DS_Store, Thumbs.db, node_modules/.
   `.gitattributes`: `* text=auto eol=lf`, `*.bat text eol=crlf`, görseller binary.
2. `git init -b main` (repo değilse). Mantıklı commit'ler: başlangıç kiti → backend → frontend → testler → README.
   Commit öncesi `git status` ile .env, token ve > 5 MB dosya olmadığını doğrula.
3. Push'tan ÖNCE bana sor: repo adı (öneri: orman-yangin-mesh), public/private, GitHub kullanıcı/organizasyon.
4. `gh auth status` başarılıysa: `gh repo create <ad> --<public|private> --source=. --remote=origin --push`.
   Değilse kurulum ve giriş komutlarını yaz (Ubuntu: `sudo apt install gh && gh auth login`; Windows:
   `winget install GitHub.cli` → `gh auth login`) ya da elle: `git remote add origin https://github.com/<kullanıcı>/<ad>.git`
   → `git push -u origin main`. Push başarısını `git ls-remote origin` ile doğrula ve repo URL'sini ver.

====================================================================================================
## 15. TESLİM RAPORU
====================================================================================================
Bitince kısaca yaz: ne yapıldı · çalıştırma komutları (Linux/Windows) · test sonuçları · repo URL'si ·
bilinen eksikler · elle kontrol listesi. Uzun özet yazma.
````
