"""Olay akışı enjekte saatle: alarm → olay → mesh → MERKEZ → 3 kurum "İletildi"; yükselme, kapatma,
sıfırlama, rotasız bekleme, sanal yangın, demo çakmak senaryosu ve komut yönlendirme."""
import pytest

from conftest import RECORDING
from server.mesh import neighbors
from server.protocol import FLAG_SILENCED
from server.state import Origin

HW = Origin("serial", "/dev/ttyUSB0")


def tel(seq, up, alarm=0, flags=0, gas=320, base=320, t=24.0, kind="periodic", node=1):
    return (f'{{"type":"tel","node":{node},"via":"rf","kind":"{kind}","seq":{seq},"up":{up},"t":{t:.1f},"h":45.0,'
            f'"gas":{gas},"base":{base},"alarm":{alarm},"flags":{flags},"vbat":0,"txf":0,"lq":100}}')


def run(engine, clock, seconds, step=0.02):
    for _ in range(int(round(seconds / step))):
        clock.advance(step)
        engine.tick()


def of(events, kind):
    return [e for e in events if e["type"] == kind]


def test_alarm_to_three_agencies_with_injected_clock(engine_factory):
    engine, clock, events = engine_factory()
    engine.ingest_line(tel(1, 60_000), HW)
    run(engine, clock, 1.0)
    engine.ingest_line(tel(2, 61_000, alarm=1, gas=1100, kind="event"), HW)

    (created,) = of(events, "incident_created")
    inc = created["incident"]
    route, hops = inc["route"], inc["hops"]
    assert route[:2] == ["F1", "F2"] and route[-1] == "MERKEZ" and 5 <= hops <= 9
    assert inc["rf_hops"] == 1 and inc["lora_hops"] == hops - 1
    assert inc["latency_ms"] == pytest.approx(inc["lora_hops"] * (205.8 + 20.0), abs=inc["lora_hops"])
    (prop,) = of(events, "propagation")
    assert prop["path"] == route and prop["hop_ms"] == 600 and prop["kinds"][0] == "rf24"

    run(engine, clock, hops * 0.6 - 0.06)
    assert not of(events, "incident_received")          # animasyon bitmeden MERKEZ almaz
    run(engine, clock, 0.1)
    (received,) = of(events, "incident_received")
    run(engine, clock, 2.0)

    order = [(e["agency"], e["status"]) for e in of(events, "dispatch")]
    assert order == [("ogm", "gonderiliyor"), ("itfaiye", "gonderiliyor"), ("muhtarlik", "gonderiliyor"),
                     ("ogm", "iletildi"), ("itfaiye", "iletildi"), ("muhtarlik", "iletildi")]
    at = {(e["agency"], e["status"]): e["at"] for e in of(events, "dispatch")}
    t0 = received["incident"]["received_at"]
    for agency, offset in (("ogm", 300), ("itfaiye", 600), ("muhtarlik", 900)):
        assert at[(agency, "gonderiliyor")] - t0 == pytest.approx(offset, abs=25)
        assert at[(agency, "iletildi")] - at[(agency, "gonderiliyor")] == pytest.approx(800, abs=25)
    (active,) = engine.incidents.active()
    assert all(d["status_text"] == "İletildi (simülasyon)" for d in active.dispatch.values())


def test_escalation_back_to_normal_and_close(engine_factory):
    engine, clock, events = engine_factory()
    engine.ingest_line(tel(1, 1000, alarm=1, gas=1100, kind="event"), HW)
    run(engine, clock, 0.3)
    engine.ingest_line(tel(2, 1300, alarm=3, gas=1100, t=33.0, kind="event"), HW)
    assert engine.incidents.active()[0].alarm_peak == 3
    run(engine, clock, 0.3)
    engine.ingest_line(tel(3, 1600, alarm=0, kind="event"), HW)
    (inc,) = engine.incidents.active()
    assert inc.alarm == 0 and inc.source_normal_at is not None          # olay açık kalır
    engine.ingest_line(tel(4, 1900, alarm=1, gas=1100, kind="event"), HW)
    assert len(engine.incidents.active()) == 1 and len(of(events, "incident_created")) == 1
    texts = " | ".join(e["text"] for e in engine.logs)
    assert "alarm yükseldi GAZ → YANGIN" in texts and "kaynak normale döndü" in texts and "yeniden başladı" in texts
    assert engine.close_incident(inc.id)["ok"]
    assert not engine.incidents.active() and of(events, "incident_closed")


def test_reset_cancels_pending_timers_and_restarts_numbering(engine_factory):
    engine, clock, events = engine_factory()
    engine.ingest_line(tel(1, 1000, alarm=1, gas=1100, kind="event"), HW)
    run(engine, clock, 1.0)
    engine.reset()
    run(engine, clock, 10.0)
    assert not of(events, "incident_received") and not of(events, "dispatch")
    assert not engine.incidents.active()
    engine.ingest_line(tel(2, 12_000, alarm=0, kind="event"), HW)
    engine.ingest_line(tel(3, 12_300, alarm=1, gas=1100, kind="event"), HW)
    assert engine.incidents.active()[0].id == 1


def test_incident_waits_without_route_and_resumes_when_node_enabled(engine_factory, app_config):
    engine, clock, events = engine_factory()
    first_relay = engine.route["path"][2]                    # F1 → F2 → [ilk röle] → …
    around_f2 = [n for n in neighbors(app_config.links, "F2") if n.startswith("N")]
    assert first_relay in around_f2
    for n in around_f2:
        assert engine.toggle_node(n)["ok"]
    assert engine.route is None
    assert "Rota yok" in engine.logs[-1]["text"]
    engine.ingest_line(tel(1, 1000, alarm=1, gas=1100, kind="event"), HW)
    (inc,) = engine.incidents.active()
    assert inc.status == "rota_yok" and not of(events, "propagation")
    engine.toggle_node(first_relay)
    assert any(f"{first_relay} yeniden etkin. Rota yeniden hesaplandı" in e["text"] for e in engine.logs)
    assert of(events, "propagation") and inc.status == "yolda" and first_relay in inc.route


def test_disable_node_reroutes_and_logs(engine_factory):
    engine, clock, events = engine_factory()
    before = engine.route["path"]
    victim = before[2]
    engine.toggle_node(victim)
    after = engine.route["path"]
    assert victim not in after and after[0] == "F1" and after[-1] == "MERKEZ"
    msg = engine.logs[-1]["text"]
    assert msg.startswith(f"{victim} devre dışı bırakıldı. Rota yeniden hesaplandı: F1 → ") and "hop)" in msg
    assert of(events, "route")[-1]["disabled"] == [victim]
    assert not engine.toggle_node("F1")["ok"]                # fiziksel düğüm kapatılamaz


def test_virtual_node_simulated_fire(engine_factory):
    engine, clock, events = engine_factory()
    assert engine.simulate_fire("N07")["ok"]
    (inc,) = engine.incidents.active()
    assert inc.source == "N07" and inc.tag == "SİMÜLE" and inc.route[0] == "N07" and not inc.can_silence
    assert engine.nodes["N07"].status() == "YANGIN"
    engine.close_incident(inc.id)
    assert engine.nodes["N07"].status() == "NORMAL"


def _alarm_track(engine, clock, seconds, step=0.02):
    track, last = [], engine.nodes["F1"].alarm
    for _ in range(int(round(seconds / step))):
        clock.advance(step)
        engine.tick()
        a = engine.nodes["F1"].alarm
        if a != last:
            track.append((clock.now(), a))
            last = a
    return track


def test_demo_lighter_scenario_follows_firmware_logic(engine_factory):
    engine, clock, events = engine_factory()
    engine.start_demo()
    run(engine, clock, 50.0)                                 # 45 s MQ ısınması geçsin
    assert engine.nodes["F1"].status() == "NORMAL" and not engine.nodes["F1"].flags & 1
    t0 = clock.now()
    engine.trigger_lighter()
    track = _alarm_track(engine, clock, 2.0)
    assert track and track[0][1] == 1 and track[0][0] - t0 <= 0.5        # gaz 300 ms'de taban+800 mV
    assert engine.command("1", "silence")["ok"]
    run(engine, clock, 1.0)
    assert engine.nodes["F1"].flags & FLAG_SILENCED                       # F1 onayı: flags&4
    track += _alarm_track(engine, clock, 50.0)
    alarms = [a for _, a in track]
    t_fire = next(t for t, a in track if a == 3) - t0
    t_clear = next(t for t, a in track if a == 0) - t0
    assert 6.0 <= t_fire <= 12.0 and 25.0 <= t_clear <= 45.0 and alarms[-1] == 0
    assert len(of(events, "incident_created")) == 1
    assert engine.incidents.active()[0].tag == "SİMÜLE"
    assert any("onayladı: siren susturuldu" in e["text"] for e in engine.logs)


def test_demo_trigger_during_warmup_skips_warmup(engine_factory):
    engine, clock, events = engine_factory()
    engine.start_demo()
    run(engine, clock, 10.0)
    t0 = clock.now()
    engine.trigger_lighter()
    track = _alarm_track(engine, clock, 1.0)
    assert track and track[0][0] - t0 <= 0.6
    assert any("ısınması simülasyon için atlandı" in e["text"] for e in engine.logs)


def test_command_routing_to_hardware_bridge(engine_factory):
    engine, clock, events = engine_factory()
    sent = []
    engine.command_sink = lambda line: sent.append(line) or True
    engine.set_source("hardware", "Donanım: /dev/ttyUSB0")
    assert engine.command("1", "silence")["ok"] and sent == ["silence 1"]
    assert engine.command("all", "test")["ok"] and sent[-1] == "test all"
    assert not engine.command("9", "silence")["ok"]
    engine.command_sink = None
    assert engine.command("1", "silence") == {"ok": False, "message": "bağlı köprü yok"}


def test_simulated_trigger_over_hardware_is_labelled_and_real_alarm_wins(engine_factory):
    engine, clock, events = engine_factory()
    sent = []
    engine.command_sink = lambda line: sent.append(line) or True
    engine.set_source("hardware", "Donanım: /dev/ttyUSB0")
    seq = 100

    def real_tick(alarm=0):
        nonlocal seq
        seq += 1
        engine.ingest_line(tel(seq, seq * 1000, alarm=alarm, gas=1100 if alarm else 320), HW)
        run(engine, clock, 0.5)

    real_tick()
    engine.trigger_lighter()
    for _ in range(4):
        real_tick()                                          # gerçek F1 normal satırları simülasyonu bozmaz
    f1 = engine.nodes["F1"]
    assert f1.alarm == 1 and f1.origin_kind == "sim" and "SİMÜLE" in f1.badges()
    (inc,) = engine.incidents.active()
    assert inc.tag == "SİMÜLE"
    engine.command("1", "silence")
    assert sent == []                                        # komut sanal köprüye gitti
    real_tick(alarm=1)                                       # gerçek alarm her zaman öncelikli
    assert engine.overlay is None and f1.origin_kind == "serial"
    assert any("Gerçek F1 alarmı geldi" in e["text"] for e in engine.logs)


def test_replay_source_plays_recording_timing(engine_factory):
    engine, clock, events = engine_factory()
    engine.start_replay(RECORDING)
    run(engine, clock, 62.0, step=0.05)
    assert engine.nodes["F1"].status() == "NORMAL"
    run(engine, clock, 0.4, step=0.05)
    assert engine.nodes["F1"].status() == "GAZ" and "REPLAY" in engine.nodes["F1"].badges()
    assert engine.incidents.active()[0].tag == "REPLAY"
