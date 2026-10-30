#!/usr/bin/env python3
"""Demo öncesi harita karolarını önbelleğe alır — salon Wi-Fi'ına güvenmeyin.

bbox (+%10 pay) için DEM z9–15 ve uydu z9–16 karolarını simulation/data/tiles/ altına indirir
(area.json → prefetch). Zaten inmiş karolar atlanır; yarıda kesilirse yeniden çalıştırın.
Karolar üçüncü taraf lisanslıdır: önbellek commit edilmez.

Kullanım:  python simulation/tools/prefetch_tiles.py [--layers dem,imagery] [--area …] [--workers 6]
           (OpenTopoMap da istenirse: --layers dem,imagery,topo)
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parents[1]
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))


def plan(area, layers: list[str]) -> list[tuple[str, int, int, int]]:
    from server.terrain import pad_bbox, tiles_for_bbox

    box = pad_bbox(area.bbox.model_dump(), area.pad_ratio)
    pf = area.prefetch
    ranges = {"dem": pf.dem_zooms, "imagery": pf.imagery_zooms, "topo": pf.imagery_zooms}
    jobs = []
    for layer in layers:
        z0, z1 = ranges[layer]
        for z in range(z0, z1 + 1):
            border = pf.low_zoom_border if z <= 12 else 0   # eğik kamerada ufuk düşük zoom karosu ister
            jobs += [(layer, z, x, y) for x, y in tiles_for_bbox(box, z, border)]
    return jobs


def main() -> int:
    import importlib.util

    if not all(importlib.util.find_spec(m) for m in ("httpx", "pydantic")):
        import run   # proje sanal ortamında yeniden başlat
        run.reexec_in_venv()
    from server.config import DATA_DIR, DEFAULT_AREA, load_area
    from server.tiles import LAYERS, TileCache, fetch_sync, make_sync_client

    ap = argparse.ArgumentParser(description="Çevrimdışı demo için harita karolarını önbelleğe al")
    ap.add_argument("--area", type=Path, default=DEFAULT_AREA)
    ap.add_argument("--layers", default="dem,imagery", help="virgüllü: dem, imagery, topo")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    layers = [l.strip() for l in args.layers.split(",") if l.strip()]
    bad = [l for l in layers if l not in LAYERS]
    if bad:
        print(f"Bilinmeyen katman: {', '.join(bad)} (geçerli: {', '.join(LAYERS)})")
        return 2
    area = load_area(args.area)
    cache = TileCache(DATA_DIR / "tiles")
    jobs = plan(area, layers)
    todo = [j for j in jobs if not cache.has(*j)]
    print(f"{area.title}: {len(jobs)} karo planlandı ({', '.join(layers)}); önbellekte {len(jobs) - len(todo)}, "
          f"indirilecek {len(todo)}")

    local = threading.local()

    def fetch(job):
        if not hasattr(local, "client"):
            local.client = make_sync_client()
        return job, fetch_sync(local.client, cache, *job) is not None

    failed = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(fetch, j) for j in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            job, ok = fut.result()
            if not ok:
                failed.append(job)
            if i % 50 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} · hata {len(failed)} · {time.monotonic() - t0:.0f} s", flush=True)

    total_bytes = 0
    per_layer: dict[str, list[int]] = {}
    for j in jobs:
        p = cache.path(*j)
        if p.is_file():
            size = p.stat().st_size
            total_bytes += size
            per_layer.setdefault(j[0], [0, 0])
            per_layer[j[0]][0] += 1
            per_layer[j[0]][1] += size
    for layer, (n, size) in per_layer.items():
        print(f"  {layer:8s} {n:5d} karo  {size / 1e6:7.1f} MB")
    print(f"Toplam: {sum(v[0] for v in per_layer.values())} karo, {total_bytes / 1e6:.1f} MB → {cache.root}")
    if failed:
        print(f"UYARI: {len(failed)} karo indirilemedi (internet?). Komutu yeniden çalıştırın.")
        return 1
    print("Hazır: harita bu bölgede internetsiz açılır.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
