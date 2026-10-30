"""Gerçek arazi: AWS Terrain Tiles (Terrarium kodlaması) çözme, karo matematiği ve
yükseklik örnekleme. Sentetik / üretilmiş arazi yoktur; her değer DEM karosundan gelir.

    yükseklik_m = (R × 256 + G + B / 256) − 32768
"""
from __future__ import annotations

import io
import math
from typing import Callable, Optional

from PIL import Image

from .mesh import haversine_m

TILE_SIZE = 256

TileFetch = Callable[[int, int, int], Optional[bytes]]   # (z, x, y) → PNG baytları ya da None


class TerrainError(RuntimeError):
    pass


def terrarium_to_m(r: int, g: int, b: int) -> float:
    return (r * 256.0 + g + b / 256.0) - 32768.0


def m_to_terrarium(elev_m: float) -> tuple[int, int, int]:
    """Testler ve sentetik karolar için ters dönüşüm (1/256 m çözünürlük)."""
    v = round((elev_m + 32768.0) * 256.0)
    r, rem = divmod(v, 256 * 256)
    g, b = divmod(rem, 256)
    return int(r), int(g), int(b)


# ---------------------------------------------------------------------------
#  Web Mercator karo matematiği (XYZ / "slippy map")
# ---------------------------------------------------------------------------
def lonlat_to_global_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = (2 ** z) * TILE_SIZE
    lat_r = math.radians(lat)
    gx = (lon + 180.0) / 360.0 * n
    gy = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return gx, gy


def global_px_to_lonlat(gx: float, gy: float, z: int) -> tuple[float, float]:
    n = (2 ** z) * TILE_SIZE
    lon = gx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * gy / n))))
    return lon, lat


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    gx, gy = lonlat_to_global_px(lon, lat, z)
    last = 2 ** z - 1
    return min(last, max(0, int(gx // TILE_SIZE))), min(last, max(0, int(gy // TILE_SIZE)))


def tile_bounds(x: int, y: int, z: int) -> dict:
    west, north = global_px_to_lonlat(x * TILE_SIZE, y * TILE_SIZE, z)
    east, south = global_px_to_lonlat((x + 1) * TILE_SIZE, (y + 1) * TILE_SIZE, z)
    return {"south": south, "west": west, "north": north, "east": east}


def tiles_for_bbox(bbox: dict, z: int, border: int = 0) -> list[tuple[int, int]]:
    x0, y0 = lonlat_to_tile(bbox["west"], bbox["north"], z)
    x1, y1 = lonlat_to_tile(bbox["east"], bbox["south"], z)
    last = 2 ** z - 1
    xs = range(max(0, x0 - border), min(last, x1 + border) + 1)
    ys = range(max(0, y0 - border), min(last, y1 + border) + 1)
    return [(x, y) for x in xs for y in ys]


def pad_bbox(bbox: dict, ratio: float) -> dict:
    dlat = (bbox["north"] - bbox["south"]) * ratio
    dlon = (bbox["east"] - bbox["west"]) * ratio
    return {
        "south": bbox["south"] - dlat,
        "west": bbox["west"] - dlon,
        "north": bbox["north"] + dlat,
        "east": bbox["east"] + dlon,
    }


def meters_per_pixel(lat: float, z: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)


def decode_terrarium_png(data: bytes) -> list[float]:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.size != (TILE_SIZE, TILE_SIZE):
        raise TerrainError(f"beklenmeyen karo boyutu {img.size}")
    raw = img.tobytes()
    return [raw[i] * 256.0 + raw[i + 1] + raw[i + 2] / 256.0 - 32768.0 for i in range(0, len(raw), 3)]


# ---------------------------------------------------------------------------
#  Karolardan yükseklik mozaiği
# ---------------------------------------------------------------------------
class DemGrid:
    """Tek zoom seviyesindeki Terrarium karolarının mozaiği; bilinear yükseklik örnekleme."""

    def __init__(self, z: int, px0: int, py0: int, width: int, height: int, values: list[float]) -> None:
        self.z = z
        self.px0 = px0          # mozaiğin sol üst köşesinin global piksel koordinatı
        self.py0 = py0
        self.width = width
        self.height = height
        self.values = values    # satır öncelikli, width × height

    @classmethod
    def from_bbox(cls, bbox: dict, z: int, fetch: TileFetch) -> "DemGrid":
        tiles = tiles_for_bbox(bbox, z)
        xs = sorted({x for x, _ in tiles})
        ys = sorted({y for _, y in tiles})
        width, height = len(xs) * TILE_SIZE, len(ys) * TILE_SIZE
        values = [0.0] * (width * height)
        for ty_i, ty in enumerate(ys):
            for tx_i, tx in enumerate(xs):
                data = fetch(z, tx, ty)
                if data is None:
                    raise TerrainError(f"DEM karosu alınamadı: {z}/{tx}/{ty} (internet ya da önbellek gerekli)")
                tile = decode_terrarium_png(data)
                for row in range(TILE_SIZE):
                    dst = (ty_i * TILE_SIZE + row) * width + tx_i * TILE_SIZE
                    values[dst:dst + TILE_SIZE] = tile[row * TILE_SIZE:(row + 1) * TILE_SIZE]
        return cls(z, xs[0] * TILE_SIZE, ys[0] * TILE_SIZE, width, height, values)

    def px_of(self, lat: float, lon: float) -> tuple[float, float]:
        gx, gy = lonlat_to_global_px(lon, lat, self.z)
        return gx - self.px0, gy - self.py0

    def lonlat_of(self, fx: float, fy: float) -> tuple[float, float]:
        """Yerel piksel merkezinden (fx + 0.5) coğrafi koordinat."""
        return global_px_to_lonlat(self.px0 + fx + 0.5, self.py0 + fy + 0.5, self.z)

    def at(self, ix: int, iy: int) -> float:
        ix = min(self.width - 1, max(0, ix))
        iy = min(self.height - 1, max(0, iy))
        return self.values[iy * self.width + ix]

    def elevation(self, lat: float, lon: float) -> float:
        fx, fy = self.px_of(lat, lon)
        fx -= 0.5   # piksel değeri piksel merkezini temsil eder
        fy -= 0.5
        ix, iy = math.floor(fx), math.floor(fy)
        tx, ty = fx - ix, fy - iy
        v00, v10 = self.at(ix, iy), self.at(ix + 1, iy)
        v01, v11 = self.at(ix, iy + 1), self.at(ix + 1, iy + 1)
        return (v00 * (1 - tx) + v10 * tx) * (1 - ty) + (v01 * (1 - tx) + v11 * tx) * ty

    def cell_size_m(self, lat: float) -> float:
        return meters_per_pixel(lat, self.z)

    def profile(self, lat1: float, lon1: float, lat2: float, lon2: float, step_m: float = 30.0) -> list[tuple[float, float]]:
        total = haversine_m(lat1, lon1, lat2, lon2)
        n = max(2, int(math.ceil(total / step_m)) + 1)
        out = []
        for i in range(n):
            f = i / (n - 1)
            lat = lat1 + (lat2 - lat1) * f
            lon = lon1 + (lon2 - lon1) * f
            out.append((total * f, self.elevation(lat, lon)))
        return out
