"""Yapılandırma: bölge (area.json), kurumlar (agencies.json), düğümler (nodes.json) ve
komut satırı ayarları. Başka bölge = yeni area JSON dosyası (--area), kod değişmez."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .mesh import LoRaParams, MeshLink, MeshParams, build_links

SIM_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SIM_DIR.parent
CONFIG_DIR = SIM_DIR / "config"
DATA_DIR = SIM_DIR / "data"
WEB_DIR = SIM_DIR / "web"
DEFAULT_AREA = CONFIG_DIR / "area.json"
DEFAULT_AGENCIES = CONFIG_DIR / "agencies.json"
DEFAULT_REPLAY = REPO_DIR / "docs" / "ornek_seri_kayit.tsv"


class BBox(BaseModel):
    south: float
    west: float
    north: float
    east: float


class Camera(BaseModel):
    center: tuple[float, float]
    zoom: float = 12.8
    pitch: float = 60.0
    bearing: float = 0.0


class MapCfg(BaseModel):
    min_zoom: float = 11
    max_zoom: float = 16
    terrain_exaggeration: float = 1.3
    imagery_saturation: float = -0.3


class PrefetchCfg(BaseModel):
    dem_zooms: tuple[int, int] = (9, 15)
    imagery_zooms: tuple[int, int] = (9, 16)
    low_zoom_border: int = 2


class LayoutCfg(BaseModel):
    seed: int = 2026
    dem_zoom: int = 13
    virtual_count: int = 16
    min_spacing_m: float = 600
    f2_distance_m: tuple[float, float] = (300, 500)
    min_elevation_m: float = 50
    edge_margin_m: float = 250
    target_hops: tuple[int, int] = (5, 9)
    attempts: int = 400


class LosCfg(BaseModel):
    enabled: bool = False
    fresnel_clearance: float = 0.6
    k_factor: float = 4.0 / 3.0
    short_link_m: float = 800
    blocked_penalty_km: float = 1.0
    step_m: float = 30


class LoraCfg(BaseModel):
    sf: int = 9
    bw_hz: int = 125_000
    cr: int = 1
    preamble: int = 8
    crc: bool = True
    explicit_header: bool = True
    payload_bytes: int = 24
    processing_ms: float = 20.0


class MeshCfg(BaseModel):
    range_m: float = 2500
    weak_ratio: float = 0.8
    weak_penalty_km: float = 0.5
    mast_m: float = 4
    freq_mhz: float = 868
    los: LosCfg = Field(default_factory=LosCfg)
    lora: LoraCfg = Field(default_factory=LoraCfg)
    hop_animation_ms: int = 600

    def params(self) -> MeshParams:
        return MeshParams(
            range_m=self.range_m,
            weak_ratio=self.weak_ratio,
            weak_penalty_km=self.weak_penalty_km,
            blocked_penalty_km=self.los.blocked_penalty_km,
            los_short_link_m=self.los.short_link_m,
        )

    def lora_params(self) -> LoRaParams:
        return LoRaParams(**self.lora.model_dump())


class Area(BaseModel):
    id: str
    title: str
    description: str = ""
    bbox: BBox
    pad_ratio: float = 0.10
    camera: Camera
    map: MapCfg = Field(default_factory=MapCfg)
    prefetch: PrefetchCfg = Field(default_factory=PrefetchCfg)
    nodes_file: str = "nodes.json"
    layout: LayoutCfg = Field(default_factory=LayoutCfg)
    mesh: MeshCfg = Field(default_factory=MeshCfg)


def load_area(path: Path = DEFAULT_AREA) -> Area:
    return Area.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def nodes_path_for(area: Area, area_path: Path = DEFAULT_AREA) -> Path:
    return Path(area_path).resolve().parent / area.nodes_file


def load_agencies(path: Path = DEFAULT_AGENCIES) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@dataclass
class Settings:
    source: str = "auto"                 # auto | serial | demo | replay
    port: Optional[str] = None
    baud: int = 115200
    http_port: int = 8000
    host: str = "127.0.0.1"
    no_browser: bool = False
    replay_file: Optional[Path] = None
    replay_loop: bool = False
    area: Path = DEFAULT_AREA
    data_dir: Path = DATA_DIR
    session_log: bool = True

    @property
    def tiles_dir(self) -> Path:
        return self.data_dir / "tiles"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


@dataclass
class AppConfig:
    area: Area
    area_path: Path
    nodes: list[dict]
    links: list[MeshLink]
    agencies: dict
    layout_info: dict = field(default_factory=dict)

    def node(self, node_id: str) -> Optional[dict]:
        for n in self.nodes:
            if n["id"] == node_id:
                return n
        return None


def ensure_nodes_file(area: Area, area_path: Path, tiles_dir: Path) -> Path:
    """nodes.json yoksa gerçek DEM'den üretir (internet ya da karo önbelleği gerekir)."""
    path = nodes_path_for(area, area_path)
    if path.is_file():
        return path
    from tools.generate_layout import generate_nodes_file  # yalnızca gerektiğinde

    print(f"{path.name} bulunamadı; gerçek DEM'den üretiliyor…")
    generate_nodes_file(area, path, tiles_dir)
    return path


def load_app_config(settings: Settings) -> AppConfig:
    area = load_area(settings.area)
    path = ensure_nodes_file(area, settings.area, settings.tiles_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data["nodes"]
    if data.get("links"):
        links = [MeshLink.from_dict(d) for d in data["links"]]
    else:
        links = build_links(nodes, area.mesh.params())
    info = {k: data.get(k) for k in ("seed", "dem", "checks")}
    return AppConfig(area=area, area_path=Path(settings.area), nodes=nodes, links=links,
                     agencies=load_agencies(), layout_info=info)
