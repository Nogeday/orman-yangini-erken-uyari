"""Terrarium çözme, karo matematiği ve DEM mozaiği (sentetik karo + varsa gerçek önbellek)."""
import io

import pytest
from PIL import Image

from conftest import SIM_DIR
from server import terrain
from server.terrain import DemGrid, m_to_terrarium, terrarium_to_m


@pytest.mark.parametrize("rgb, elev", [
    ((128, 0, 0), 0.0),
    ((128, 100, 128), 100.5),
    ((129, 44, 0), 300.0),
    ((136, 191, 0), 2239.0),     # 136·256 + 191 − 32768
    ((127, 255, 0), -1.0),       # deniz seviyesinin 1 m altı
    ((0, 0, 0), -32768.0),
])
def test_terrarium_decode_known_values(rgb, elev):
    assert terrarium_to_m(*rgb) == pytest.approx(elev)


@pytest.mark.parametrize("elev", [-412.25, 0.0, 1.5, 1590.0, 2242.75, 8848.0])
def test_terrarium_roundtrip(elev):
    assert terrarium_to_m(*m_to_terrarium(elev)) == pytest.approx(elev, abs=1 / 256)


def test_tile_math_contains_point_and_bbox_tiles():
    lat, lon, z = 40.729984, 39.616994, 13          # MERKEZ
    x, y = terrain.lonlat_to_tile(lon, lat, z)
    b = terrain.tile_bounds(x, y, z)
    assert b["south"] <= lat <= b["north"] and b["west"] <= lon <= b["east"]
    bbox = {"south": 40.660, "west": 39.600, "north": 40.740, "east": 39.720}
    tiles = terrain.tiles_for_bbox(bbox, 13)
    assert len(tiles) == len(set(tiles)) == 9          # 3 × 3 karo
    gx, gy = terrain.lonlat_to_global_px(lon, lat, z)
    lon2, lat2 = terrain.global_px_to_lonlat(gx, gy, z)
    assert (lon2, lat2) == pytest.approx((lon, lat), abs=1e-9)


def _synthetic_tile(fn) -> bytes:
    img = Image.new("RGB", (256, 256))
    img.putdata([m_to_terrarium(fn(px, py)) for py in range(256) for px in range(256)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_demgrid_bilinear_on_synthetic_tiles():
    z = 12
    x0, y0 = terrain.lonlat_to_tile(39.66, 40.70, z)
    # Her karoda yükseklik = 1000 + global x pikseli → doğuya doğru düzgün eğim
    def fetch(zz, x, y):
        return _synthetic_tile(lambda px, py: 1000.0 + ((x - x0) * 256 + px) * 1.0)
    bbox = terrain.tile_bounds(x0, y0, z)
    grid = DemGrid.from_bbox({k: v + (1e-6 if k in ("south", "west") else -1e-6) for k, v in bbox.items()}, z, fetch)
    lat, lon = (bbox["south"] + bbox["north"]) / 2, bbox["west"] + (bbox["east"] - bbox["west"]) * 0.25
    gx, _ = terrain.lonlat_to_global_px(lon, lat, z)
    expected = 1000.0 + (gx - x0 * 256 - 0.5)
    assert grid.elevation(lat, lon) == pytest.approx(expected, abs=0.01)
    prof = grid.profile(lat, bbox["west"] + 1e-4, lat, bbox["east"] - 1e-4, step_m=200)
    assert prof[0][0] == 0 and all(b[1] > a[1] for a, b in zip(prof, prof[1:]))


def test_missing_tile_raises_clear_error():
    with pytest.raises(terrain.TerrainError):
        DemGrid.from_bbox({"south": 40.66, "west": 39.6, "north": 40.67, "east": 39.61}, 12, lambda z, x, y: None)


def test_real_cached_dem_matches_layout_elevations():
    """Önbellekte gerçek DEM varsa (prefetch/generate_layout sonrası) düğüm rakımlarını yeniden üretir."""
    import json

    from server.tiles import TileCache

    cache = TileCache(SIM_DIR / "data" / "tiles")
    data = json.loads((SIM_DIR / "config" / "nodes.json").read_text(encoding="utf-8"))
    bbox = {"south": 40.660, "west": 39.600, "north": 40.740, "east": 39.720}
    if not all(cache.has("dem", 13, x, y) for x, y in terrain.tiles_for_bbox(bbox, 13)):
        pytest.skip("DEM önbelleği yok (python simulation/tools/prefetch_tiles.py)")
    grid = DemGrid.from_bbox(bbox, 13, lambda z, x, y: cache.read("dem", z, x, y))
    for n in data["nodes"]:
        assert grid.elevation(n["lat"], n["lon"]) == pytest.approx(n["rakim_m"], abs=1.0)
        assert 300 < n["rakim_m"] < 3500                    # Doğu Karadeniz dağları, deniz değil
    # nodes.json'daki her LoRa linkinin görüş hattı durumu aynı DEM'den birebir yeniden üretilir
    from server import mesh

    nodes = {n["id"]: n for n in data["nodes"]}
    for l in data["links"]:
        if l["kind"] != "lora" or l["los"] == "unchecked":
            continue
        a, b = nodes[l["a"]], nodes[l["b"]]
        res = mesh.los_check(grid.profile(a["lat"], a["lon"], b["lat"], b["lon"], 30.0),
                             a["rakim_m"] + a["direk_m"], b["rakim_m"] + b["direk_m"], 868e6)
        assert res.clear == (l["los"] == "clear"), (l["a"], l["b"])
