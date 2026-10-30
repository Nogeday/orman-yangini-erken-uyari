"""Ayrıştırıcı, firmware biçimi, tekrar eleme ve gerçek kayıt üzerinde durum zaman çizelgesi."""
from conftest import RECORDING
from server.demo import load_recording
from server.protocol import Deduper, Hello, Link, Telemetry, format_tel, parse_line
from server.state import Origin

ITEMS = load_recording(RECORDING)


def feed_until(engine, clock, t_s, start_index=0):
    """Kayıttaki satırları kayıttaki zamanlarla (≤ t_s) motora verir; sonraki indeksi döner."""
    i = start_index
    origin = Origin("replay", "kayıt")
    while i < len(ITEMS) and ITEMS[i][0] / 1000.0 <= t_s + 1e-9:
        clock.set(ITEMS[i][0] / 1000.0)
        engine.ingest_line(ITEMS[i][1], origin)
        engine.tick()
        i += 1
    clock.set(t_s)
    engine.tick()
    return i


def test_recording_every_line_parses():
    assert len(ITEMS) == 503
    parsed = [parse_line(line) for _, line in ITEMS]
    assert all(m is not None for m in parsed)
    assert sum(isinstance(m, Telemetry) for m in parsed) == 459
    assert isinstance(parsed[0], Hello) and parsed[0].role == "gateway"


def test_formatter_reproduces_firmware_lines_exactly():
    kinds = {"periodic": 0, "event": 1, "boot": 2}
    for _, line in ITEMS:
        m = parse_line(line)
        if not isinstance(m, Telemetry):
            continue
        again = format_tel(
            node=m.node, via=m.via, ptype=kinds[m.kind], seq=m.seq, up=m.up,
            temp_x10=-32768 if m.t is None else round(m.t * 10), hum_x10=0xFFFF if m.h is None else round(m.h * 10),
            gas=m.gas, base=m.base, alarm=m.alarm, flags=m.flags, vbat=m.vbat, txf=m.txf,
            lq=-1 if m.lq is None else m.lq, rf_ok=-1 if m.rf_ok is None else m.rf_ok,
        )
        assert again == line


def test_non_json_and_broken_lines_are_ignored():
    for junk in [
        "", "   ", "ets Jun  8 2016 00:22:57", "rst:0x1 (POWERON_RESET),boot:0x13",
        '{"type":"tel"', "[1,2,3]", '{"type":"tel","node":1}', '{"type":"bilinmeyen"}',
        '{"type":"tel","node":1,"via":"rf","seq":1,"up":1,"gas":1,"base":1,"alarm":9}',
        b"\xff\xfe{garbage",
    ]:
        assert parse_line(junk) is None


def test_bytes_and_crlf_lines_parse():
    m = parse_line(b'{"type":"link","node":1,"state":"lost","age_ms":3501}\r\n')
    assert isinstance(m, Link) and m.state == "lost" and m.age_ms == 3501


def test_deduper_drops_repeats_within_window_only():
    m = parse_line(ITEMS[3][1])
    d = Deduper(window_s=10.0)
    assert not d.is_duplicate(m, now=100.0)
    assert d.is_duplicate(m, now=100.2)          # aynı paket (ör. radyo + USB)
    assert not d.is_duplicate(m, now=111.0)      # pencere geçti: yeniden başlayan düğüm elenmez
    other = parse_line(ITEMS[5][1])
    assert not d.is_duplicate(other, now=111.1)


def test_engine_drops_same_packet_from_rf_and_usb(engine_factory):
    engine, clock, events = engine_factory()
    rf = '{"type":"tel","node":1,"via":"rf","kind":"periodic","seq":7,"up":7000,"t":24.0,"h":45.0,"gas":615,"base":659,"alarm":0,"flags":1,"vbat":0,"txf":0,"lq":100}'
    usb = '{"type":"tel","node":1,"via":"usb","kind":"periodic","seq":7,"up":7000,"t":24.0,"h":45.0,"gas":615,"base":659,"alarm":0,"flags":1,"vbat":0,"txf":0,"rf_ok":1}'
    engine.ingest_line(rf, Origin("serial", "/dev/ttyUSB0"))
    clock.advance(0.01)
    engine.ingest_line(usb, Origin("serial", "/dev/ttyUSB1"))
    assert sum(1 for e in events if e["type"] == "telemetry") == 1


def test_recording_timeline_gas_lost_and_back(engine_factory):
    engine, clock, events = engine_factory()
    f1 = engine.nodes["F1"]
    i = feed_until(engine, clock, 62.2)
    assert f1.status() == "NORMAL"
    i = feed_until(engine, clock, 62.3, i)
    assert f1.status() == "GAZ"                       # 62.3 s: çakmak gazı → GAZ
    created = [e for e in events if e["type"] == "incident_created"]
    assert len(created) == 1 and created[0]["incident"]["source"] == "F1"
    i = feed_until(engine, clock, 105.0, i)
    assert f1.status() == "ISI"                       # son veri 101.8 s; henüz 3.5 s dolmadı
    i = feed_until(engine, clock, 105.301, i)
    assert f1.status() == "BAGLANTI_YOK"              # 105.3 s: köprü "link lost"
    i = feed_until(engine, clock, 108.1, i)
    assert f1.status() == "ISI"                       # 108.1 s: geri geldi
    feed_until(engine, clock, 108.2, i)
    assert f1.status() == "NORMAL"
    texts = [e["text"] for e in engine.logs]
    assert any("F1 onayladı: siren susturuldu" in t for t in texts)
    assert any("alarm yükseldi GAZ → YANGIN" in t for t in texts)
    assert any("kaynak normale döndü" in t for t in texts)
    inc = engine.incidents.active()[0]
    assert inc.status == "merkez_aldi" and all(d["status"] == "iletildi" for d in inc.dispatch.values())
