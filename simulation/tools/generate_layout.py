#!/usr/bin/env python3
"""Gerçek DEM'den (AWS Terrain Tiles, Terrarium) deterministik düğüm yerleşimi → config/nodes.json

Kurallar:
  F1      fiziksel saha düğümü: bbox'ın güneydoğu ucu, yerel yüksek nokta (en uzak uç)
  F2      fiziksel köprü: F1'den 300–500 m içeride, yerel yüksek nokta
  MERKEZ  kuzeybatı ucu, yerel en yüksek noktalardan biri
  N01…    sanal LoRa düğümleri: aralarında ≥ 600 m, sırt tercihli (yerel yükseklik maksimumu),
          < 50 m ya da çevresine göre çukur (vadi tabanı) hariç. Graf bağlı, her sanal düğümün
          ≥ 2 komşusu var, F1 → MERKEZ en kısa yolu 5–9 hop. Sağlanmazsa aralık gevşetilir.

Aynı seed ve aynı DEM → aynı dosya. Sonuç commit edilir; sunucu bu dosyayı okur.
Kullanım:  python simulation/tools/generate_layout.py [--area simulation/config/area.json]
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import random
import sys
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parents[1]
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

HW_PHYSICAL_F1 = "ESP32-S + nRF24 (prototip)"
HW_PHYSICAL_F2 = "ESP32 DevKit V1 + nRF24 (prototip)"
HW_VIRTUAL = "LoRa SX1262 (konsept)"
HW_CENTER = "LoRa SX1262 ağ geçidi + internet (konsept)"


class _Cell:
    __slots__ = ("r", "c", "lat", "lon", "elev", "tpi", "s", "x", "idx", "z")

    def __init__(self, r, c, lat, lon, elev, tpi):
        self.r, self.c, self.lat, self.lon, self.elev, self.tpi = r, c, lat, lon, elev, tpi
        self.s = 0.0    # F2 → MERKEZ doğrultusunda konum (0…1)
        self.x = 0.0    # doğrultuya dik uzaklık (m)
        self.idx = -1
        self.z = 0      # nodes.json'a yazılacak rakım (yuvarlanmış koordinatta bilinear, tam sayı)


def _grid(dem, bbox, step):
    """DEM'i bbox içinde step×step piksellik hücrelere ortalar."""
    from server.terrain import global_px_to_lonlat

    fx0, fy0 = dem.px_of(bbox["north"], bbox["west"])
    fx1, fy1 = dem.px_of(bbox["south"], bbox["east"])
    cols = int((fx1 - fx0) // step)
    rows = int((fy1 - fy0) // step)
    elev = [[0.0] * cols for _ in range(rows)]
    lats, lons = [0.0] * rows, [0.0] * cols
    for c in range(cols):
        lons[c] = global_px_to_lonlat(dem.px0 + fx0 + (c + 0.5) * step, 0, dem.z)[0]
    for r in range(rows):
        lats[r] = global_px_to_lonlat(0, dem.py0 + fy0 + (r + 0.5) * step, dem.z)[1]
        y0 = int(fy0 + r * step)
        row = elev[r]
        for c in range(cols):
            x0 = int(fx0 + c * step)
            acc = 0.0
            for dy in range(step):
                for dx in range(step):
                    acc += dem.at(x0 + dx, y0 + dy)
            row[c] = acc / (step * step)
    return elev, lats, lons


def _tpi(elev, rad):
    """Topoğrafik konum indeksi: hücre − (2·rad+1)² pencere ortalaması (integral görüntü)."""
    rows, cols = len(elev), len(elev[0])
    S = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for r in range(rows):
        acc = 0.0
        src, prev, dst = elev[r], S[r], S[r + 1]
        for c in range(cols):
            acc += src[c]
            dst[c + 1] = prev[c + 1] + acc
    out = [[0.0] * cols for _ in range(rows)]
    for r in range(rows):
        r0, r1 = max(0, r - rad), min(rows, r + rad + 1)
        for c in range(cols):
            c0, c1 = max(0, c - rad), min(cols, c + rad + 1)
            total = S[r1][c1] - S[r0][c1] - S[r1][c0] + S[r0][c0]
            out[r][c] = elev[r][c] - total / ((r1 - r0) * (c1 - c0))
    return out


def _ridge_mask(elev, k):
    """Sırt/tepe hücresi: dört doğrultudan (K–G, D–B, KD–GB, KB–GD) en az birinde ±k hücre
    içindeki en yüksek nokta, yani sırt çizgisinin üstü."""
    rows, cols = len(elev), len(elev[0])
    out = [[False] * cols for _ in range(rows)]
    dirs = ((0, 1), (1, 0), (1, 1), (1, -1))
    for r in range(k, rows - k):
        row = elev[r]
        for c in range(k, cols - k):
            e = row[c]
            for dr, dc in dirs:
                for t in range(1, k + 1):
                    if elev[r + dr * t][c + dc * t] > e or elev[r - dr * t][c - dc * t] > e:
                        break
                else:
                    out[r][c] = True
                    break
    return out


def _thin(cells, radius_cells):
    """Çevresinden en yüksek (TPI) olan önce: radius_cells içinde ikinci aday bırakma."""
    keep, buckets = [], {}
    b = max(1, radius_cells)
    for p in sorted(cells, key=lambda p: (-p.tpi, p.r, p.c)):
        br, bc = p.r // b, p.c // b
        clash = False
        for rr in (br - 1, br, br + 1):
            for cc in (bc - 1, bc, bc + 1):
                for q in buckets.get((rr, cc), ()):
                    if (q.r - p.r) ** 2 + (q.c - p.c) ** 2 < radius_cells ** 2:
                        clash = True
                        break
                if clash:
                    break
            if clash:
                break
        if not clash:
            keep.append(p)
            buckets.setdefault((br, bc), []).append(p)
    return sorted(keep, key=lambda p: (p.r, p.c))


def _weighted_pick(rng, pool, weight):
    ws = [weight(p) for p in pool]
    total = sum(ws)
    if total <= 0:
        return None
    t = rng.random() * total
    for p, w in zip(pool, ws):
        t -= w
        if t <= 0:
            return p
    return pool[-1]


def generate(area, dem) -> dict:
    from server import mesh

    L, M = area.layout, area.mesh
    bbox = area.bbox.model_dump()
    lat0 = (bbox["south"] + bbox["north"]) / 2
    step = 2
    cell_m = dem.cell_size_m(lat0) * step
    elev, lats, lons = _grid(dem, bbox, step)
    rows, cols = len(elev), len(elev[0])
    tpi = _tpi(elev, max(2, round(300 / cell_m)))
    ridge_mask = _ridge_mask(elev, max(1, round(60 / cell_m)))
    margin = max(1, math.ceil(L.edge_margin_m / cell_m))

    cells = []      # kenar payı içindeki tüm hücreler (F2 için)
    ridge = []      # sırt üstü + çevresinden ≥ 8 m yüksek (vadi tabanı/çukur değil) + ≥ min_elevation
    for r in range(margin, rows - margin):
        for c in range(margin, cols - margin):
            cell = _Cell(r, c, lats[r], lons[c], elev[r][c], tpi[r][c])
            cells.append(cell)
            if ridge_mask[r][c] and tpi[r][c] >= 8.0 and elev[r][c] >= L.min_elevation_m:
                ridge.append(cell)
    cands = _thin(ridge, max(1, round(150 / cell_m)))
    for i, p in enumerate(cands):
        p.idx = i
    if len(cands) < L.virtual_count + 3:
        raise RuntimeError(f"yeterli sırt noktası yok ({len(cands)})")

    def dist(a, b):
        return mesh.haversine_m(a.lat, a.lon, b.lat, b.lon)

    class _P:
        def __init__(self, lat, lon):
            self.lat, self.lon = lat, lon

    se, nw = _P(bbox["south"], bbox["east"]), _P(bbox["north"], bbox["west"])
    mid = _P(lat0, (bbox["west"] + bbox["east"]) / 2)

    # F1: güneydoğu ucunda, MERKEZ köşesine en uzak sırt noktası
    near = []
    for radius in (1000, 1500, 2000, 3000, 5000):
        near = [p for p in cands if dist(p, se) <= radius]
        if near:
            break
    f1 = max(near, key=lambda p: (dist(p, nw) / 1000.0 + 0.005 * p.tpi, -p.r, -p.c))

    # F2: F1'den 300–500 m içeride (bbox merkezine doğru ±60°), o halkadaki en yüksek sırt noktası
    dmin, dmax = L.f2_distance_m
    inward = mesh.bearing_deg(f1.lat, f1.lon, mid.lat, mid.lon)

    def inward_ok(p):
        b = mesh.bearing_deg(f1.lat, f1.lon, p.lat, p.lon)
        return abs((b - inward + 180) % 360 - 180) <= 60

    ring = [p for p in ridge if dmin <= dist(p, f1) <= dmax and inward_ok(p)] or \
           [p for p in cells if dmin <= dist(p, f1) <= dmax and inward_ok(p)]
    if not ring:
        raise RuntimeError("F2 için uygun nokta yok")
    f2 = max(ring, key=lambda p: (p.elev, -p.r, -p.c))

    # MERKEZ: kuzeybatı ucunda yerel en yüksek nokta
    for radius in (2500, 3500, 5000):
        near = [p for p in cands if dist(p, nw) <= radius]
        if near:
            break
    center = max(near, key=lambda p: (p.elev, -p.r, -p.c))

    # F2 → MERKEZ koridoru için yerel metrik çerçeve
    kx = math.cos(math.radians(lat0)) * 111_320.0
    ky = 110_540.0
    ax, ay = f2.lon * kx, f2.lat * ky
    bx, by = center.lon * kx, center.lat * ky
    vx, vy = bx - ax, by - ay
    vlen = math.hypot(vx, vy)
    for p in cands:
        px, py = p.lon * kx - ax, p.lat * ky - ay
        p.s = (px * vx + py * vy) / (vlen * vlen)
        p.x = (vx * py - vy * px) / vlen

    rng_m = M.range_m
    bins = max(3, round(vlen / (0.8 * rng_m)))
    fixed = [f1, f2, center]

    # Aday + sabit noktalar arası mesafe matrisi (deneme döngüsü binlerce kez sorar)
    pts = list(cands)
    for p in fixed:
        if p.idx < 0 or p.idx >= len(pts) or pts[p.idx] is not p:
            p.idx = len(pts)
            pts.append(p)
    D = [[0.0] * len(pts) for _ in pts]
    for i, a in enumerate(pts):
        for j in range(i + 1, len(pts)):
            b = pts[j]
            D[i][j] = D[j][i] = mesh.haversine_m(a.lat, a.lon, b.lat, b.lon)

    def dist(a, b):  # noqa: F811 — matrisli sürüm
        return D[a.idx][b.idx]

    # Görüş hattı, çıktıya yazılacak değerlerle (6 ondalık koordinat, tam sayı rakım) hesaplanır:
    # nodes.json'daki her linkin durumu aynı DEM'den birebir yeniden üretilebilir.
    for p in pts:
        p.z = round(dem.elevation(round(p.lat, 6), round(p.lon, 6)))

    def ridge(p):
        return min(1.0, max(0.05, p.tpi / 60.0))

    los_cache: dict[tuple, object] = {}

    def los_cached(a, b):
        key = (a["_key"], b["_key"]) if a["_key"] < b["_key"] else (b["_key"], a["_key"])
        res = los_cache.get(key)
        if res is None:
            prof = dem.profile(a["lat"], a["lon"], b["lat"], b["lon"], M.los.step_m)
            res = mesh.los_check(prof, a["_elev"] + M.mast_m, b["_elev"] + M.mast_m,
                                 M.freq_mhz * 1e6, M.los.k_factor, M.los.fresnel_clearance)
            los_cache[key] = res
        return res

    los_fn = los_cached if M.los.enabled else None

    def as_node(nid, p, **extra):
        return {"id": nid, "lat": round(p.lat, 6), "lon": round(p.lon, 6), "_key": (p.idx,), "_elev": p.z,
                "_cell": p, **extra}

    def node_dicts(chosen):
        order = sorted(chosen, key=lambda p: (p.s, p.x))
        out = [as_node("F1", f1, uplink="F2"), as_node("F2", f2)]
        out += [as_node(f"N{i:02d}", p) for i, p in enumerate(order, 1)]
        out.append(as_node("MERKEZ", center))
        return out

    # Görüş hattı açıkken: aday çiftleri arası link grafı (menzil + LoS kuralı). F1 yalnızca F2'ye bağlanır.
    vis = None
    if M.los.enabled:
        vis = [set() for _ in pts]
        for i in range(len(pts)):
            if i == f1.idx:
                continue
            for j in range(i + 1, len(pts)):
                if j == f1.idx or D[i][j] > rng_m:
                    continue
                res = los_cached(as_node("a", pts[i]), as_node("b", pts[j]))
                if res.clear or D[i][j] <= M.los.short_link_m:
                    vis[i].add(j)
                    vis[j].add(i)

    rejects: dict[str, int] = {}

    def reject(reason):
        rejects[reason] = rejects.get(reason, 0) + 1

    def evaluate(chosen):
        nodes = node_dicts(chosen)
        links = mesh.build_links(nodes, M.params(), los_fn)
        ids = [n["id"] for n in nodes]
        if not mesh.is_connected(links, ids):
            return reject("bağlı değil")
        adj = mesh.adjacency(links)
        degrees = {n: len(adj.get(n, [])) for n in ids}
        if any(degrees[n] < 2 for n in ids if n.startswith("N")):
            return reject("komşu < 2")
        sp = mesh.shortest_path(links, "F1", "MERKEZ")
        if sp is None:
            return reject("rota yok")
        path, _ = sp
        hops = len(path) - 1
        if not (L.target_hops[0] <= hops <= L.target_hops[1]):
            return reject(f"hop {hops}")
        on_route = [n for n in path if n.startswith("N")]
        robust = sum(1 for n in on_route if mesh.shortest_path(links, "F1", "MERKEZ", {n})) / max(1, len(on_route))
        nn = []
        for p in chosen:
            nn.append(min(dist(p, q) for q in fixed + chosen if q is not p))
        spread = min(1.0, (sum(nn) / len(nn)) / 1500.0)
        score = (sum(ridge(p) for p in chosen) / len(chosen)) + robust + 0.3 * (1 - abs(hops - 7) / 4) + 0.2 * spread
        return {"score": score, "nodes": nodes, "links": links, "path": path, "robust": robust,
                "degrees": degrees, "min_spacing": min(nn)}

    def spread_w(p, chosen):
        d = min(dist(p, q) for q in fixed + chosen)
        return ridge(p) * (0.3 + min(d, 2000.0) / 2000.0)

    def sample_corridor(rng, spacing):
        """F2 → MERKEZ koridoru dilimlerinden ikişer düğüm, kalanı kapsama için."""
        chosen: list[_Cell] = []

        def ok(p):
            return all(dist(p, q) >= spacing for q in fixed + chosen)

        for b in range(bins):
            s0 = 0.03 + b * (0.94 / bins)
            s1 = s0 + 0.94 / bins
            pool = [p for p in cands if s0 <= p.s < s1 and abs(p.x) <= 2200]
            for _k in range(2):
                avail = [p for p in pool if ok(p)]
                p = _weighted_pick(rng, avail, lambda p: ridge(p) * math.exp(-0.5 * (p.x / 1100.0) ** 2)) if avail else None
                if p is not None:
                    chosen.append(p)
        while len(chosen) < L.virtual_count:
            pool = [p for p in cands if ok(p) and sum(1 for q in fixed + chosen if dist(p, q) <= 0.85 * rng_m) >= 2]
            if not pool:
                break
            chosen.append(_weighted_pick(rng, pool, lambda p: spread_w(p, chosen)))
        return chosen

    def sample_visibility(rng, spacing):
        """Görüş hattı grafında: F2 → MERKEZ omurgası (rastgele ağırlıklı Dijkstra, hop başına sabit maliyet),
        her omurga düğümü için iki komşusunu da gören bir baypas düğümü, kalanı ≥ 2 görünür komşulu sırtlar."""
        chosen: list[_Cell] = []
        taken: set[int] = set()

        def ok(p):
            return all(dist(p, q) >= spacing for q in fixed + chosen)

        def add(p):
            chosen.append(p)
            taken.add(p.idx)

        noise: dict[tuple[int, int], float] = {}

        def weight(i, j):
            key = (i, j) if i < j else (j, i)
            if key not in noise:
                noise[key] = rng.random()
            return D[i][j] / 1000.0 + 0.8 + 0.6 * noise[key]

        src, dst = f2.idx, center.idx
        best_d = {src: 0.0}
        prev: dict[int, int] = {}
        heap = [(0.0, src)]
        done: set[int] = set()
        while heap:
            du, u = heapq.heappop(heap)
            if u in done:
                continue
            done.add(u)
            if u == dst:
                break
            for v in sorted(vis[u]):
                if v != dst and not all(dist(pts[v], q) >= spacing for q in fixed):
                    continue
                nd = du + weight(u, v)
                if nd < best_d.get(v, math.inf):
                    best_d[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))
        if dst not in best_d:
            return None
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        for i in path[1:-1]:
            if not ok(pts[i]):
                return None
            add(pts[i])
        for k in range(1, len(path) - 1):
            if len(chosen) >= L.virtual_count:
                break
            u, v, w = path[k - 1], path[k], path[k + 1]
            common = [pts[x] for x in sorted(vis[u] & vis[w]) if x not in (u, v, w) and x not in taken and ok(pts[x])]
            if common:
                add(_weighted_pick(rng, common, ridge))
        anchors = [f2.idx, center.idx]
        while len(chosen) < L.virtual_count:
            linked = anchors + [q.idx for q in chosen]
            pool = [p for p in cands if p.idx not in taken and p.idx != f1.idx and ok(p)
                    and sum(1 for q in linked if q in vis[p.idx]) >= 2]
            if not pool:
                break
            add(_weighted_pick(rng, pool, lambda p: spread_w(p, chosen)))
        return chosen

    sampler = sample_visibility if vis is not None else sample_corridor
    best = None
    used_spacing = None
    for spacing in (L.min_spacing_m, L.min_spacing_m - 50, L.min_spacing_m - 100):
        rng = random.Random(L.seed)
        for _ in range(L.attempts):
            chosen = sampler(rng, spacing) or []
            if len(chosen) != L.virtual_count:
                reject(f"yerleşen {len(chosen)}")
                continue
            res = evaluate(chosen)
            if res and (best is None or res["score"] > best["score"]):
                best = res
        if best is not None:
            used_spacing = spacing
            break
    if best is None:
        why = ", ".join(f"{k}: {v}" for k, v in sorted(rejects.items(), key=lambda kv: -kv[1]))
        raise RuntimeError(f"kısıtları sağlayan yerleşim bulunamadı ({why}); "
                           f"aday {len(cands)}, koridor {vlen / 1000:.1f} km, {bins} dilim, "
                           f"F1 {f1.lat:.4f},{f1.lon:.4f} F2 {f2.lat:.4f},{f2.lon:.4f} M {center.lat:.4f},{center.lon:.4f}")

    # ---- çıktı ----
    path = best["path"]
    names = {
        "F1": ("physical", "sensor", "F1 · Saha sensör düğümü", HW_PHYSICAL_F1),
        "F2": ("physical", "gateway", "F2 · Köprü düğümü", HW_PHYSICAL_F2),
        "MERKEZ": ("center", "center", "MERKEZ · Yangın gözetleme ve komuta", HW_CENTER),
    }
    out_nodes = []
    for n in best["nodes"]:
        cell = n["_cell"]
        tur, rol, ad, hw = names.get(n["id"], ("virtual", "relay", f"{n['id']} · Sanal LoRa rölesi", HW_VIRTUAL))
        rec = {
            "id": n["id"], "tur": tur, "rol": rol, "ad": ad,
            "lat": round(n["lat"], 6), "lon": round(n["lon"], 6),
            "rakim_m": cell.z,
            "tpi_m": round(cell.tpi, 1),
            "direk_m": M.mast_m, "donanim": hw,
        }
        if n.get("uplink"):
            rec["uplink"] = n["uplink"]
        out_nodes.append(rec)
    final_links = mesh.build_links(out_nodes, M.params(), los_fn and (lambda a, b: los_fn(_with_key(a, best), _with_key(b, best))))
    sp = mesh.shortest_path(final_links, "F1", "MERKEZ")
    assert sp is not None and sp[0] == path, "yuvarlanmış koordinatlarla rota değişti"
    v_ids = [n["id"] for n in out_nodes if n["tur"] == "virtual"]
    adj = mesh.adjacency(final_links)
    min_sp = min(
        mesh.haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        for i, a in enumerate(out_nodes) for b in out_nodes[i + 1:]
        if {a["id"], b["id"]} != {"F1", "F2"}   # F1–F2 kuralı ayrı: 300–500 m
    )
    return {
        "_not": "simulation/tools/generate_layout.py ile gerçek DEM'den üretildi. Elle düzenlemeyin; yeniden üretin.",
        "area": area.id,
        "seed": L.seed,
        "dem": {"kaynak": "AWS Terrain Tiles (Terrarium)", "zoom": dem.z, "hucre_m": round(cell_m, 1)},
        "kurallar": {
            "range_m": M.range_m, "min_spacing_m": used_spacing, "f2_distance_m": list(L.f2_distance_m),
            "target_hops": list(L.target_hops), "los": M.los.enabled,
        },
        "checks": {
            "connected": True,
            "min_spacing_m": round(min_sp, 1),
            "f1_f2_m": round(mesh.haversine_m(f1.lat, f1.lon, f2.lat, f2.lon), 1),
            "candidates": len(cands),
            "min_virtual_degree": min(len(adj[v]) for v in v_ids),
            "route": path,
            "route_hops": len(path) - 1,
            "route_km": round(mesh.path_length_m(final_links, path) / 1000.0, 2),
            "bypassable_route_nodes": f"{round(best['robust'] * len([p for p in path if p.startswith('N')]))}/"
                                      f"{len([p for p in path if p.startswith('N')])}",
        },
        "camera_suggestion": {
            "center": [round((f1.lon + center.lon) / 2, 5), round((f1.lat + center.lat) / 2, 5)],
            "bearing": round(mesh.bearing_deg(f1.lat, f1.lon, center.lat, center.lon), 1),
        },
        "nodes": out_nodes,
        "links": [l.to_dict() for l in final_links],
    }


def _with_key(rec, best):
    for n in best["nodes"]:
        if n["id"] == rec["id"]:
            return {**rec, "_key": n["_key"], "_elev": n["_elev"]}
    raise KeyError(rec["id"])


def markdown_table(data: dict) -> str:
    from server import mesh

    links = [mesh.MeshLink.from_dict(l) for l in data["links"]]
    adj = mesh.adjacency(links)
    tur = {"physical": "fiziksel", "virtual": "sanal", "center": "merkez"}
    rows = ["| ID | Tür | Enlem | Boylam | Rakım (m) | Komşu | Donanım |", "|---|---|---:|---:|---:|---:|---|"]
    for n in data["nodes"]:
        rows.append(f"| {n['id']} | {tur[n['tur']]} | {n['lat']:.6f} | {n['lon']:.6f} | {n['rakim_m']} | "
                    f"{len(adj.get(n['id'], []))} | {n['donanim']} |")
    ch = data["checks"]
    rows.append("")
    rows.append(f"Varsayılan rota: {' → '.join(ch['route'])} ({ch['route_hops']} hop, {ch['route_km']} km). "
                f"Sanal düğümler arası en küçük aralık {ch['min_spacing_m']} m; F1–F2 {ch['f1_f2_m']} m; "
                f"her sanal düğümün en az {ch['min_virtual_degree']} komşusu var; rota üzerindeki sanal düğümlerin "
                f"{ch['bypassable_route_nodes']} tanesi devre dışı kalınca alternatif yol bulunuyor.")
    return "\n".join(rows)


def load_dem(area, tiles_dir: Path):
    from server.terrain import DemGrid
    from server.tiles import TileCache, fetch_sync, make_sync_client

    cache = TileCache(tiles_dir)
    with make_sync_client() as client:
        return DemGrid.from_bbox(area.bbox.model_dump(), area.layout.dem_zoom,
                                 lambda z, x, y: fetch_sync(client, cache, "dem", z, x, y))


def generate_nodes_file(area, out_path: Path, tiles_dir: Path) -> dict:
    dem = load_dem(area, tiles_dir)
    data = generate(area, dem)
    Path(out_path).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return data


def main() -> int:
    import importlib.util

    if not all(importlib.util.find_spec(m) for m in ("httpx", "PIL", "pydantic")):
        import run   # proje sanal ortamında yeniden başlat
        run.reexec_in_venv()
    from server.config import DATA_DIR, DEFAULT_AREA, load_area, nodes_path_for

    ap = argparse.ArgumentParser(description="Gerçek DEM'den düğüm yerleşimi üret (config/nodes.json)")
    ap.add_argument("--area", type=Path, default=DEFAULT_AREA)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--table", action="store_true", help="mevcut nodes.json'dan README için Markdown tablo bas")
    args = ap.parse_args()
    area = load_area(args.area)
    out = args.out or nodes_path_for(area, args.area)
    if args.table:
        print(markdown_table(json.loads(out.read_text(encoding="utf-8"))))
        return 0
    data = generate_nodes_file(area, out, DATA_DIR / "tiles")
    ch = data["checks"]
    print(f"Yazıldı: {out}")
    print(f"  düğüm {len(data['nodes'])} · link {len(data['links'])} · en küçük aralık {ch['min_spacing_m']} m · "
          f"sanal en az komşu {ch['min_virtual_degree']}")
    print(f"  rota: {' → '.join(ch['route'])} ({ch['route_hops']} hop, {ch['route_km']} km) · "
          f"atlanabilir rota düğümü {ch['bypassable_route_nodes']}")
    print(f"  kamera önerisi: {data['camera_suggestion']}")
    print(f"  {'id':7s} {'lat':>10s} {'lon':>10s} {'rakım':>6s} {'tpi':>6s}")
    for n in data["nodes"]:
        print(f"  {n['id']:7s} {n['lat']:10.6f} {n['lon']:10.6f} {n['rakim_m']:6d} {n['tpi_m']:6.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
