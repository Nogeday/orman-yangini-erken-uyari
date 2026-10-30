"""Mesh modeli — jüriye açıklanabilir ve birim testli.

1. Link kuralı: iki LoRa düğümü arası mesafe ≤ `range_m` ise link var.
   Görüş hattı (isteğe bağlı, area.json → mesh.los.enabled): DEM profilinde direk tepesinden
   direk tepesine çizgi; 868 MHz'te 1. Fresnel bölgesinin %60'ı açık olmalı, dünya eğriliği
   k = 4/3. Engelliyse ve mesafe > 800 m ise link yok; daha kısaysa cezalı "zayıf" link.
   F1 (nRF24 prototip) yalnızca köprüsü F2'ye bağlanır (`uplink`); bu hop gerçektir.
2. Link maliyeti = mesafe_km + ceza (mesafe > %80 menzil: +0.5 · görüş hattı engelli: +1.0).
3. Rota: Dijkstra (kaynak → MERKEZ), devre dışı düğümler grafa alınmaz.
4. Gecikme: LoRa hava süresi (Semtech AN1200.13) + hop başına işlem süresi.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Optional

EARTH_RADIUS_M = 6_371_008.8
SPEED_OF_LIGHT = 299_792_458.0


# ---------------------------------------------------------------------------
#  Geometri
# ---------------------------------------------------------------------------
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """1 → 2 yönü, kuzeyden saat yönünde derece (−180…180)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y))


def destination(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    d = distance_m / EARTH_RADIUS_M
    b = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


# ---------------------------------------------------------------------------
#  LoRa hava süresi — Semtech AN1200.13 (LoRa Modem Designer's Guide)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LoRaParams:
    sf: int = 9
    bw_hz: int = 125_000
    cr: int = 1                 # kodlama oranı 4/(4+cr) → 1 = 4/5
    preamble: int = 8
    crc: bool = True
    explicit_header: bool = True
    low_dr_optimize: Optional[bool] = None   # None: sembol süresi > 16 ms ise açık
    payload_bytes: int = 24
    processing_ms: float = 20.0


def lora_airtime_ms(
    payload_bytes: int,
    sf: int = 9,
    bw_hz: int = 125_000,
    cr: int = 1,
    preamble: int = 8,
    crc: bool = True,
    explicit_header: bool = True,
    low_dr_optimize: Optional[bool] = None,
) -> float:
    """T_paket = T_önsöz + T_yük

    T_sym    = 2^SF / BW
    T_önsöz  = (n_önsöz + 4.25) · T_sym
    n_yük    = 8 + max(ceil((8·PL − 4·SF + 28 + 16·CRC − 20·IH) / (4·(SF − 2·DE))) · (CR + 4), 0)
    """
    t_sym = (2 ** sf) / bw_hz
    de = (t_sym > 0.016) if low_dr_optimize is None else bool(low_dr_optimize)
    ih = 0 if explicit_header else 1
    t_preamble = (preamble + 4.25) * t_sym
    num = 8 * payload_bytes - 4 * sf + 28 + 16 * (1 if crc else 0) - 20 * ih
    den = 4 * (sf - 2 * (1 if de else 0))
    n_payload = 8 + max(math.ceil(num / den) * (cr + 4), 0)
    return (t_preamble + n_payload * t_sym) * 1000.0


def lora_airtime_for(p: LoRaParams) -> float:
    return lora_airtime_ms(p.payload_bytes, p.sf, p.bw_hz, p.cr, p.preamble, p.crc, p.explicit_header, p.low_dr_optimize)


def hop_latency_ms(p: LoRaParams) -> float:
    return lora_airtime_for(p) + p.processing_ms


# ---------------------------------------------------------------------------
#  Görüş hattı (LoS) ve Fresnel bölgesi
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LosResult:
    clear: bool
    worst_ratio: float      # en kötü noktada açıklık / 1. Fresnel yarıçapı (negatif: arazi çizginin üstünde)
    worst_at_m: float
    samples: int


def fresnel_radius_m(d1_m: float, d2_m: float, freq_hz: float) -> float:
    lam = SPEED_OF_LIGHT / freq_hz
    return math.sqrt(lam * d1_m * d2_m / (d1_m + d2_m))


def earth_bulge_m(d1_m: float, d2_m: float, k: float = 4.0 / 3.0) -> float:
    return d1_m * d2_m / (2.0 * k * EARTH_RADIUS_M)


def los_check(
    profile: list[tuple[float, float]],
    h_a_m: float,
    h_b_m: float,
    freq_hz: float = 868e6,
    k: float = 4.0 / 3.0,
    clearance: float = 0.6,
) -> LosResult:
    """profile: A'dan uzaklık (m) ve zemin yüksekliği (m) örnekleri, uç noktalar dahil.
    h_a_m / h_b_m: anten yükseklikleri (deniz seviyesinden, zemin + direk)."""
    total = profile[-1][0]
    worst = math.inf
    worst_at = 0.0
    for d1, ground in profile:
        d2 = total - d1
        if d1 <= 0.0 or d2 <= 0.0:
            continue
        line_h = h_a_m + (h_b_m - h_a_m) * d1 / total
        obstacle = ground + earth_bulge_m(d1, d2, k)
        ratio = (line_h - obstacle) / fresnel_radius_m(d1, d2, freq_hz)
        if ratio < worst:
            worst, worst_at = ratio, d1
    # Ara örnek yoksa (çok kısa link) engel de yoktur: worst = inf → açık.
    return LosResult(clear=worst >= clearance, worst_ratio=worst, worst_at_m=worst_at, samples=len(profile))


# ---------------------------------------------------------------------------
#  Linkler ve rota
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MeshParams:
    range_m: float = 2500.0
    weak_ratio: float = 0.8
    weak_penalty_km: float = 0.5
    blocked_penalty_km: float = 1.0
    los_short_link_m: float = 800.0


@dataclass
class MeshLink:
    a: str
    b: str
    distance_m: float
    kind: str               # "rf24" (gerçek, F1 → F2) | "lora" (simüle)
    cost: float
    weak: bool = False
    los: str = "unchecked"  # "clear" | "blocked" | "unchecked"
    clearance: Optional[float] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["distance_m"] = round(self.distance_m, 1)
        d["cost"] = round(self.cost, 4)
        if self.clearance is not None:
            d["clearance"] = round(self.clearance, 3) if math.isfinite(self.clearance) else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MeshLink":
        return cls(
            a=d["a"], b=d["b"], distance_m=float(d["distance_m"]), kind=d.get("kind", "lora"),
            cost=float(d["cost"]), weak=bool(d.get("weak", False)), los=d.get("los", "unchecked"),
            clearance=d.get("clearance"),
        )


LosFn = Callable[[dict, dict], LosResult]


def build_links(nodes: list[dict], params: MeshParams, los_fn: Optional[LosFn] = None) -> list[MeshLink]:
    """`uplink` alanı olan düğüm (F1) yalnızca o düğüme nRF24 ile bağlanır; diğerleri LoRa."""
    by_id = {n["id"]: n for n in nodes}
    links: list[MeshLink] = []
    for n in nodes:
        up = n.get("uplink")
        if up:
            u = by_id[up]
            d = haversine_m(n["lat"], n["lon"], u["lat"], u["lon"])
            links.append(MeshLink(n["id"], u["id"], d, "rf24", d / 1000.0))
    lora = [n for n in nodes if not n.get("uplink")]
    for i, a in enumerate(lora):
        for b in lora[i + 1:]:
            d = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            if d > params.range_m:
                continue
            weak = d > params.weak_ratio * params.range_m
            cost = d / 1000.0 + (params.weak_penalty_km if weak else 0.0)
            los, clearance = "unchecked", None
            if los_fn is not None:
                res = los_fn(a, b)
                clearance = res.worst_ratio
                if res.clear:
                    los = "clear"
                else:
                    if d > params.los_short_link_m:
                        continue
                    los, weak = "blocked", True
                    cost += params.blocked_penalty_km
            links.append(MeshLink(a["id"], b["id"], d, "lora", cost, weak, los, clearance))
    return links


def adjacency(links: Iterable[MeshLink], disabled: Iterable[str] = ()) -> dict[str, list[tuple[str, float]]]:
    off = set(disabled)
    adj: dict[str, list[tuple[str, float]]] = {}
    for l in links:
        if l.a in off or l.b in off:
            continue
        adj.setdefault(l.a, []).append((l.b, l.cost))
        adj.setdefault(l.b, []).append((l.a, l.cost))
    for v in adj.values():
        v.sort()
    return adj


def shortest_path(
    links: Iterable[MeshLink], src: str, dst: str, disabled: Iterable[str] = ()
) -> Optional[tuple[list[str], float]]:
    """Dijkstra. Eşit maliyette düğüm kimliğine göre deterministik."""
    off = set(disabled)
    if src in off or dst in off:
        return None
    adj = adjacency(links, off)
    dist: dict[str, float] = {src: 0.0}
    prev: dict[str, str] = {}
    heap: list[tuple[float, str]] = [(0.0, src)]
    done: set[str] = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        if u == dst:
            break
        for v, w in adj.get(u, ()):
            nd = d + w
            if nd < dist.get(v, math.inf) - 1e-12:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    if dst not in dist:
        return None
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    path.reverse()
    return path, dist[dst]


def neighbors(links: Iterable[MeshLink], node_id: str, disabled: Iterable[str] = ()) -> list[str]:
    return [v for v, _ in adjacency(links, disabled).get(node_id, [])]


def is_connected(links: Iterable[MeshLink], node_ids: Iterable[str], disabled: Iterable[str] = ()) -> bool:
    off = set(disabled)
    ids = [n for n in node_ids if n not in off]
    if not ids:
        return True
    adj = adjacency(links, off)
    seen = {ids[0]}
    stack = [ids[0]]
    while stack:
        u = stack.pop()
        for v, _ in adj.get(u, ()):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return all(n in seen for n in ids)


def path_length_m(links: Iterable[MeshLink], path: list[str]) -> float:
    by_pair = {frozenset((l.a, l.b)): l for l in links}
    return sum(by_pair[frozenset((a, b))].distance_m for a, b in zip(path, path[1:]))
