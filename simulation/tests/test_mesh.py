"""Mesh modeli: LoRa hava süresi, link kuralı, Dijkstra + yeniden rota, görüş hattı, üretilmiş yerleşim."""
import json

import pytest

from conftest import SIM_DIR
from server import mesh
from server.mesh import MeshParams, build_links, lora_airtime_ms, shortest_path


def test_lora_airtime_sf9_125k_24b():
    # Semtech AN1200.13: SF9, BW 125 kHz, CR 4/5, önsöz 8, CRC açık, açık başlık, 24 bayt
    assert lora_airtime_ms(24, sf=9, bw_hz=125_000) == pytest.approx(205.8, abs=1.0)


def test_lora_airtime_cross_checks():
    assert lora_airtime_ms(24, sf=7) == pytest.approx(61.7, abs=0.2)
    # SF12/125 kHz: sembol 32.8 ms > 16 ms → düşük veri hızı optimizasyonu kendiliğinden açık
    assert lora_airtime_ms(24, sf=12) == pytest.approx(1482.8, abs=0.5)
    assert lora_airtime_ms(48, sf=9) > lora_airtime_ms(24, sf=9)


def test_haversine_one_degree_latitude():
    assert mesh.haversine_m(40.0, 39.6, 41.0, 39.6) == pytest.approx(111_195, rel=1e-3)


def test_link_rule_distance_only_and_uplink():
    # Enlem 40.7'de doğu–batı hattı: A'lar ~2.2 km aralıklı (> %80 menzil → zayıf), B, A3'e ~1 km
    nodes = [{"id": f"A{i}", "lat": 40.7, "lon": 39.6 + i * 0.026} for i in range(4)]
    nodes.append({"id": "B", "lat": 40.7, "lon": 39.6 + 3 * 0.026 + 0.012})
    nodes.insert(0, {"id": "S", "lat": 40.7, "lon": 39.6 - 0.004, "uplink": "A0"})
    links = build_links(nodes, MeshParams(range_m=2500))
    by_pair = {frozenset((l.a, l.b)): l for l in links}
    assert set(by_pair) == {frozenset(p) for p in [("S", "A0"), ("A0", "A1"), ("A1", "A2"), ("A2", "A3"), ("A3", "B")]}
    assert by_pair[frozenset(("S", "A0"))].kind == "rf24"           # S yalnızca köprüsüne (nRF24)
    for pair in [("A0", "A1"), ("A1", "A2"), ("A2", "A3")]:
        l = by_pair[frozenset(pair)]
        assert l.kind == "lora" and l.weak and l.cost == pytest.approx(l.distance_m / 1000 + 0.5)
    short = by_pair[frozenset(("A3", "B"))]
    assert not short.weak and short.cost == pytest.approx(short.distance_m / 1000)


def _diamond():
    L = mesh.MeshLink
    return [L("A", "B", 1000, "lora", 1.0), L("B", "D", 1000, "lora", 1.0),
            L("A", "C", 1500, "lora", 1.5), L("C", "D", 1500, "lora", 1.5)]


def test_dijkstra_and_reroute_when_node_disabled():
    links = _diamond()
    assert shortest_path(links, "A", "D") == (["A", "B", "D"], 2.0)
    assert shortest_path(links, "A", "D", disabled={"B"}) == (["A", "C", "D"], 3.0)
    assert shortest_path(links, "A", "D", disabled={"B", "C"}) is None
    assert shortest_path(links, "A", "D", disabled={"D"}) is None


def test_los_blocked_by_synthetic_hill():
    # İki sırt (1100 m) arası 2 km; ortada 1200 m'lik sentetik tepe → görüş hattı kapalı
    prof_valley = [(d, 1100.0 if d in (0, 2000) else 900.0) for d in range(0, 2001, 50)]
    prof_hill = [(d, 1200.0 if 900 <= d <= 1100 else g) for d, g in prof_valley]
    assert mesh.los_check(prof_valley, 1104.0, 1104.0).clear
    blocked = mesh.los_check(prof_hill, 1104.0, 1104.0)
    assert not blocked.clear and blocked.worst_ratio < 0 and 900 <= blocked.worst_at_m <= 1100


def test_los_fresnel_and_earth_curvature():
    # 868 MHz, 2 km linkin ortası: 1. Fresnel yarıçapı ≈ 13.1 m; dünya kabarıklığı (k=4/3) ≈ 6 cm
    assert mesh.fresnel_radius_m(1000, 1000, 868e6) == pytest.approx(13.14, abs=0.05)
    assert mesh.earth_bulge_m(1000, 1000) == pytest.approx(0.0589, abs=0.001)
    # Düz arazi + 4 m direk: %60 açıklık sağlanmaz (gerçek hayatta da sorun)
    flat = [(d, 0.0) for d in range(0, 2001, 50)]
    assert not mesh.los_check(flat, 4.0, 4.0).clear


def test_los_rule_drops_long_blocked_links_keeps_short_ones():
    nodes = [{"id": "A", "lat": 40.7, "lon": 39.60}, {"id": "B", "lat": 40.7, "lon": 39.62},
             {"id": "C", "lat": 40.7, "lon": 39.6085}]
    blocked = mesh.LosResult(clear=False, worst_ratio=-0.5, worst_at_m=100, samples=10)
    links = build_links(nodes, MeshParams(range_m=2500), los_fn=lambda a, b: blocked)
    pairs = {frozenset((l.a, l.b)): l for l in links}
    assert frozenset(("A", "B")) not in pairs                 # 1.7 km, engelli → yok
    short = pairs[frozenset(("A", "C"))]                       # ~720 m, engelli → zayıf + ceza
    assert short.los == "blocked" and short.weak and short.cost == pytest.approx(short.distance_m / 1000 + 1.0)


def test_generated_layout_meets_rules():
    data = json.loads((SIM_DIR / "config" / "nodes.json").read_text(encoding="utf-8"))
    area = json.loads((SIM_DIR / "config" / "area.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in data["nodes"]}
    virtual = [n for n in data["nodes"] if n["tur"] == "virtual"]
    assert len(virtual) == 16
    assert {"F1", "F2", "MERKEZ"} <= set(nodes)
    b = area["bbox"]
    for n in data["nodes"]:
        assert b["south"] <= n["lat"] <= b["north"] and b["west"] <= n["lon"] <= b["east"]
        assert n["rakim_m"] >= 50 and n["direk_m"] == 4
    d = lambda a, c: mesh.haversine_m(a["lat"], a["lon"], c["lat"], c["lon"])  # noqa: E731
    assert 300 <= d(nodes["F1"], nodes["F2"]) <= 500
    for v in virtual:
        assert min(d(v, o) for o in data["nodes"] if o is not v) >= 600
    # F1 güneydoğu ucunda, MERKEZ kuzeybatı ucunda
    assert nodes["F1"]["lat"] < nodes["MERKEZ"]["lat"] and nodes["F1"]["lon"] > nodes["MERKEZ"]["lon"]
    links = [mesh.MeshLink.from_dict(l) for l in data["links"]]
    assert mesh.is_connected(links, list(nodes))
    adj = mesh.adjacency(links)
    assert all(len(adj[v["id"]]) >= 2 for v in virtual)
    assert mesh.neighbors(links, "F1") == ["F2"]
    path, _ = shortest_path(links, "F1", "MERKEZ")
    assert path[:2] == ["F1", "F2"] and 5 <= len(path) - 1 <= 9
    assert path == data["checks"]["route"]


def test_every_route_node_can_be_bypassed():
    data = json.loads((SIM_DIR / "config" / "nodes.json").read_text(encoding="utf-8"))
    links = [mesh.MeshLink.from_dict(l) for l in data["links"]]
    path, _ = shortest_path(links, "F1", "MERKEZ")
    for node in path[2:-1]:
        alt = shortest_path(links, "F1", "MERKEZ", disabled={node})
        assert alt is not None and node not in alt[0]
