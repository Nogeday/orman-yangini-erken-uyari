"""FastAPI sunucusu: REST + WebSocket + karo vekili + statik arayüz (derleme adımı yok)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
import urllib.request
import webbrowser
from contextlib import asynccontextmanager
from typing import Callable, Literal, Optional, Union

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import SIM_DIR, WEB_DIR, AppConfig, Settings, load_app_config
from .serial_bridge import SerialBridge
from .state import Clock, Engine, Origin, SessionLog
from .terrain import pad_bbox
from .telegram_notify import TelegramNotifier
from .tiles import LAYERS, TileCache, TileProxy, valid_request

log = logging.getLogger("orman")
AUTO_FALLBACK_S = 6.0     # auto: donanım koptuktan bu kadar sonra DEMO'ya geç
TICK_S = 0.02


class Hub:
    """WebSocket yayın merkezi. Yavaş istemci kuyruğu taşarsa bağlantısı kapatılır;
    yeniden bağlanınca güncel anlık görüntüyü alır."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    @property
    def count(self) -> int:
        return len(self._queues)

    def publish(self, event: dict) -> None:
        if not self._queues:
            return
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        for q in list(self._queues):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)
                self._queues.discard(q)

    async def serve(self, ws: WebSocket, snapshot: Callable[[], dict]) -> None:
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._queues.add(q)
        try:
            await ws.send_text(json.dumps({"type": "snapshot", **snapshot()}, ensure_ascii=False, separators=(",", ":")))
            pump = asyncio.create_task(self._pump(ws, q))
            drain = asyncio.create_task(self._drain(ws))
            _, pending = await asyncio.wait({pump, drain}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self._queues.discard(q)

    @staticmethod
    async def _pump(ws: WebSocket, q: asyncio.Queue) -> None:
        while True:
            data = await q.get()
            if data is None:
                await ws.close()
                return
            await ws.send_text(data)

    @staticmethod
    async def _drain(ws: WebSocket) -> None:
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return


class Runtime:
    """Veri kaynaklarını yönetir: seri köprü (iş parçacığı), demo, replay ve 20 ms'lik tik."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.bridge: Optional[SerialBridge] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._tick_task: Optional[asyncio.Task] = None
        self._started = time.monotonic()
        self._hw_lost_at: Optional[float] = None
        self._scanned = False

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._started = time.monotonic()
        e, s = self.engine, self.settings
        if s.source == "demo":
            e.start_demo("DEMO", "DEMO modu: sanal F1 + F2 (donanım kullanılmıyor)")
        elif s.source == "replay":
            e.start_replay(s.replay_file, s.replay_loop)
        else:
            e.set_source("searching", "Donanım aranıyor…", "warn", "", log=False)
            self.bridge = SerialBridge(self._line_from_thread, self._status_from_thread, port=s.port, baud=s.baud)
            self.bridge.start()
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
        if self.bridge is not None:
            await asyncio.to_thread(self.bridge.stop)

    # seri iş parçacığından → olay döngüsüne
    def _line_from_thread(self, text: str, rx_wall: float, port: str) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.engine.ingest_line, text, Origin("serial", port), rx_wall)

    def _status_from_thread(self, ev: dict) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._on_status, ev)

    def _on_status(self, ev: dict) -> None:
        e = self.engine
        if ev["state"] == "connected":
            self._hw_lost_at = None
            if e.demo is not None:
                e.stop_demo()
                e.log("Donanım bulundu — DEMO durduruldu", "info")
            e.command_sink = self.bridge.send if self.bridge else None
            label = "Donanım: " + " + ".join(p for p, _ in ev["ports"])
            e.set_source("hardware", label, "ok", ev["message"])
            return
        self._scanned = True
        if ev["state"] == "lost" and self._hw_lost_at is None:
            self._hw_lost_at = time.monotonic()
        if e.demo is not None:
            e.set_source("demo", "DEMO · donanım aranıyor", "ok", ev["message"], log=False)
        elif self.settings.source == "auto" and ev["state"] == "searching":
            # ipucu, birazdan açılacak DEMO kaydında bir kez yazılır
            e.set_source("searching", "Donanım aranıyor…", "warn", ev["message"], log=False)
        elif ev["state"] == "lost":
            e.command_sink = None
            e.set_source("lost", "Donanım bağlantısı koptu — yeniden deneniyor", "warn", ev["message"])
        else:
            e.set_source("searching", "Donanım aranıyor…", "warn", ev["message"])

    async def _tick_loop(self) -> None:
        while True:
            try:
                self.engine.tick()
                self._auto_policy()
            except Exception:  # noqa: BLE001 — tik döngüsü asla ölmesin
                log.exception("tik hatası")
            await asyncio.sleep(TICK_S)

    def _auto_policy(self) -> None:
        e = self.engine
        if self.settings.source != "auto" or e.demo is not None or (self.bridge and self.bridge.connected):
            return
        now = time.monotonic()
        if self._hw_lost_at is not None:
            if now - self._hw_lost_at >= AUTO_FALLBACK_S:
                e.start_demo("DEMO · donanım aranıyor", "Donanım yok — DEMO moduna geçildi; port taraması sürüyor")
        elif self._scanned or now - self._started > 4.0:
            hint = e.source.get("message") or ""
            e.start_demo("DEMO · donanım aranıyor",
                         "Donanım bulunamadı — DEMO modu (sanal F1 + F2); 3 s'de bir port taranıyor"
                         + (f". {hint}" if hint else ""))


class CommandBody(BaseModel):
    target: Union[int, Literal["all"]] = 1
    cmd: Literal["silence", "test"] = "silence"


def public_config(cfg: AppConfig, engine: Engine) -> dict:
    a = cfg.area
    bbox = a.bbox.model_dump()
    return {
        "version": __version__,
        "area": {"id": a.id, "title": a.title, "description": a.description, "bbox": bbox,
                 "bounds": pad_bbox(bbox, a.pad_ratio), "camera": a.camera.model_dump(), "map": a.map.model_dump()},
        "nodes": cfg.nodes,
        "links": [l.to_dict() for l in cfg.links],
        "agencies": cfg.agencies.get("agencies", []),
        "mesh": {"range_m": a.mesh.range_m, "hop_ms": a.mesh.hop_animation_ms, "freq_mhz": a.mesh.freq_mhz,
                 "lora": a.mesh.lora.model_dump(), "airtime_ms": round(engine.airtime_ms, 1),
                 "hop_latency_ms": round(engine.hop_latency_ms, 1), "los": a.mesh.los.enabled},
        "layers": {k: {"attribution": v.attribution, "max_zoom": v.max_zoom} for k, v in LAYERS.items()},
        "layout": cfg.layout_info,
    }


def create_app(settings: Settings, clock: Optional[Clock] = None) -> FastAPI:
    cfg = load_app_config(settings)
    clock = clock or Clock()
    hub = Hub()
    session_log = SessionLog(settings.logs_dir, clock) if settings.session_log else None
    engine = Engine(cfg, clock=clock, emit=hub.publish, session_log=session_log)
    runtime = Runtime(settings, engine)
    tiles = TileProxy(TileCache(settings.tiles_dir))
    pub = public_config(cfg, engine)
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Telegram (gerçek kanal bildirimi) burada, olay döngüsü içinde kurulur: modül içe aktarılırken değil;
        # testlerin ortam ayarı (TELEGRAM_BOT_TOKEN="") ancak böyle geçerli olur.
        notifier = TelegramNotifier.from_env(SIM_DIR / ".env", on_result=engine.on_telegram_result)
        engine.notifier = notifier
        status = notifier.describe()
        log.info(status)
        engine.log(status, "warn" if notifier.problem else "info", tag="")
        await tiles.start()
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()
            await notifier.aclose()
            await tiles.close()
            if session_log is not None:
                session_log.close()

    app = FastAPI(title="Orman Yangını Erken Uyarı Ağı", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.engine = engine
    app.state.runtime = runtime

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # Arayüz dosyaları her açılışta doğrulansın (304); güncelleme sonrası eski JS/CSS kalmasın.
        response = await call_next(request)
        path = request.url.path
        if not path.startswith(("/tiles/", "/api/")) and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache"
        return response

    def reply(res: dict) -> JSONResponse:
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "version": __version__, "source": engine.source, "clients": hub.count,
                "uptime_s": round(time.monotonic() - started, 1),
                "tiles": {"misses": tiles.misses, "offline": time.monotonic() < tiles.offline_until},
                "session_log": str(session_log.path) if session_log and session_log.path else None}

    @app.get("/api/config")
    async def get_config() -> dict:
        return {**pub, "telegram_enabled": bool(engine.notifier is not None and engine.notifier.enabled)}

    @app.get("/api/state")
    async def get_state() -> dict:
        return engine.snapshot()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await hub.serve(ws, engine.snapshot)

    @app.post("/api/demo/trigger")
    async def demo_trigger() -> JSONResponse:
        return reply(engine.trigger_lighter())

    @app.post("/api/reset")
    async def reset() -> JSONResponse:
        return reply(engine.reset())

    @app.post("/api/command")
    async def command(body: CommandBody) -> JSONResponse:
        return reply(engine.command(str(body.target), body.cmd))

    @app.post("/api/node/{node_id}/toggle")
    async def toggle(node_id: str) -> JSONResponse:
        return reply(engine.toggle_node(node_id))

    @app.post("/api/node/{node_id}/simulate")
    async def simulate(node_id: str) -> JSONResponse:
        return reply(engine.simulate_fire(node_id))

    @app.post("/api/incident/{inc_id}/close")
    async def close_incident(inc_id: int) -> JSONResponse:
        return reply(engine.close_incident(inc_id))

    @app.get("/tiles/{layer}/{z}/{x}/{y}")
    async def tile(layer: str, z: int, x: int, y: str) -> Response:
        try:
            yi = int(y.split(".", 1)[0])
        except ValueError:
            return Response(status_code=404)
        if not valid_request(layer, z, x, yi):
            return Response(status_code=404)
        data, status = await tiles.get(layer, z, x, yi)
        if data is None:
            return Response(status_code=404, headers={"X-Tile-Status": status, "Cache-Control": "no-store"})
        return Response(content=data, media_type=LAYERS[layer].media_type,
                        headers={"Cache-Control": "public, max-age=86400", "X-Tile-Status": status})

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _bind(host: str, port: int) -> tuple[socket.socket, int]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    last: Optional[OSError] = None
    for p in range(port, port + 10):
        s = socket.socket(family, socket.SOCK_STREAM)
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, p))
            return s, p
        except OSError as exc:
            last = exc
            s.close()
    raise SystemExit(f"{host}:{port}–{port + 9} portlarından hiçbiri açılamadı ({last}). --http-port ile başka port seçin.")


def _open_browser_when_ready(url: str) -> None:
    def run() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url + "api/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        webbrowser.open(url)

    threading.Thread(target=run, name="tarayici", daemon=True).start()


def serve(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = create_app(settings)
    sock, port = _bind(settings.host, settings.http_port)
    shown_host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    url = f"http://{shown_host}:{port}/"
    if port != settings.http_port:
        print(f"{settings.http_port} portu dolu, {port} kullanılıyor.", flush=True)
    print(f"Orman Yangını Erken Uyarı Ağı — {url}   (kaynak: {settings.source}; çıkış: Ctrl+C)", flush=True)
    if not settings.no_browser:
        _open_browser_when_ready(url)
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
