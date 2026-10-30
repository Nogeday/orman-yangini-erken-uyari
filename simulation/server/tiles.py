"""Harita karosu vekili: önce disk önbelleği (simulation/data/tiles), yoksa üst kaynak.

Karolar üçüncü taraf lisanslıdır; önbellek asla commit edilmez. Üst kaynağa ulaşılamazsa
sunucu 404 döner ve bir süre (OFFLINE_BACKOFF_S) yeniden denemez — salonda internet yokken
her karo 8 s beklemesin.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

USER_AGENT = "orman-yangin-mesh/1.0 (hackathon demo)"
TIMEOUT_S = 8.0
OFFLINE_BACKOFF_S = 20.0


@dataclass(frozen=True)
class Layer:
    url: str
    ext: str
    media_type: str
    max_zoom: int
    attribution: str


LAYERS: dict[str, Layer] = {
    "dem": Layer(
        url="https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
        ext="png", media_type="image/png", max_zoom=15,
        attribution="Yükseklik: AWS Terrain Tiles (Mapzen; SRTM, GMTED ve diğer kaynaklar)",
    ),
    "imagery": Layer(
        # DİKKAT: Esri sırası {z}/{y}/{x}
        url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        ext="jpg", media_type="image/jpeg", max_zoom=16,
        attribution="Uydu görüntüsü: © Esri, Maxar, Earthstar Geographics",
    ),
    "topo": Layer(
        url="https://tile.opentopomap.org/{z}/{x}/{y}.png",
        ext="png", media_type="image/png", max_zoom=16,
        attribution="Topoğrafya: © OpenStreetMap katkıcıları, SRTM · stil © OpenTopoMap (CC-BY-SA)",
    ),
}


def valid_request(layer: str, z: int, x: int, y: int) -> bool:
    lay = LAYERS.get(layer)
    if lay is None or not (0 <= z <= lay.max_zoom):
        return False
    n = 2 ** z
    return 0 <= x < n and 0 <= y < n


def upstream_url(layer: str, z: int, x: int, y: int) -> str:
    return LAYERS[layer].url.format(z=z, x=x, y=y)


def looks_like_image(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")


class TileCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, layer: str, z: int, x: int, y: int) -> Path:
        return self.root / layer / str(z) / str(x) / f"{y}.{LAYERS[layer].ext}"

    def read(self, layer: str, z: int, x: int, y: int) -> Optional[bytes]:
        p = self.path(layer, z, x, y)
        try:
            return p.read_bytes()
        except OSError:
            return None

    def has(self, layer: str, z: int, x: int, y: int) -> bool:
        return self.path(layer, z, x, y).is_file()

    def write(self, layer: str, z: int, x: int, y: int, data: bytes) -> None:
        p = self.path(layer, z, x, y)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)


def make_sync_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def fetch_sync(client: httpx.Client, cache: TileCache, layer: str, z: int, x: int, y: int) -> Optional[bytes]:
    """Araçlar (prefetch, layout) için: önbellek → üst kaynak → önbelleğe yaz."""
    data = cache.read(layer, z, x, y)
    if data is not None:
        return data
    try:
        r = client.get(upstream_url(layer, z, x, y))
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or not looks_like_image(r.content):
        return None
    cache.write(layer, z, x, y, r.content)
    return r.content


class TileProxy:
    """Sunucu tarafı asenkron vekil."""

    def __init__(self, cache: TileCache, max_concurrency: int = 6) -> None:
        self.cache = cache
        self._client: Optional[httpx.AsyncClient] = None
        self._sem = asyncio.Semaphore(max_concurrency)
        self.offline_until = 0.0
        self.misses = 0
        self.last_miss_at = 0.0

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, follow_redirects=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _miss(self) -> None:
        self.misses += 1
        self.last_miss_at = time.time()

    async def get(self, layer: str, z: int, x: int, y: int) -> tuple[Optional[bytes], str]:
        """(veri, durum) — durum: cache | upstream | offline | missing"""
        data = await asyncio.to_thread(self.cache.read, layer, z, x, y)
        if data is not None:
            return data, "cache"
        if self._client is None or time.monotonic() < self.offline_until:
            self._miss()
            return None, "offline"
        async with self._sem:
            try:
                r = await self._client.get(upstream_url(layer, z, x, y))
            except httpx.HTTPError:
                self.offline_until = time.monotonic() + OFFLINE_BACKOFF_S
                self._miss()
                return None, "offline"
        if r.status_code != 200 or not looks_like_image(r.content):
            self._miss()
            return None, "missing"
        await asyncio.to_thread(self.cache.write, layer, z, x, y, r.content)
        return r.content, "upstream"
