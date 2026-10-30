"""Duman testi: gerçek sunucuyu (run.py) demo modunda rastgele portta başlatır, WebSocket'e bağlanır,
/api/demo/trigger çağırır; 15 s içinde incident_received ve 3 kurumun "iletildi" dispatch olayını,
ardından "Sahadaki sireni sustur" komutunun F1'den flags&4 onayıyla döndüğünü doğrular."""
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from websockets.sync.client import connect

from conftest import SIM_DIR


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    port = _free_port()
    env = {**os.environ, "ORMAN_NO_VENV": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(SIM_DIR / "run.py"), "--source", "demo", "--no-browser", "--no-log",
         "--host", "127.0.0.1", "--http-port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            pytest.fail("sunucu açılmadı:\n" + (proc.stdout.read() if proc.stdout else ""))
        time.sleep(0.2)
    yield base, port
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _recv(ws, timeout=1.0):
    try:
        return json.loads(ws.recv(timeout=timeout))
    except TimeoutError:
        return None


def test_demo_trigger_reaches_three_agencies_and_silence_is_acknowledged(server):
    base, port = server
    cfg = httpx.get(base + "/api/config").json()
    assert len(cfg["nodes"]) == 19 and cfg["mesh"]["airtime_ms"] == pytest.approx(205.8, abs=1)
    assert cfg["telegram_enabled"] is False          # testler gerçek kanala asla mesaj göndermez

    with connect(f"ws://127.0.0.1:{port}/ws", open_timeout=5) as ws:
        first = _recv(ws, 5)
        assert first["type"] == "snapshot" and first["source"]["mode"] == "demo"
        assert httpx.post(base + "/api/demo/trigger", timeout=5).json()["ok"]

        seen, delivered = set(), set()
        t_end = time.monotonic() + 15
        while time.monotonic() < t_end and len(delivered) < 3:
            ev = _recv(ws)
            if ev is None:
                continue
            seen.add(ev["type"])
            if ev["type"] == "dispatch" and ev["status"] == "iletildi":
                delivered.add(ev["agency"])
        assert {"incident_created", "propagation", "incident_received"} <= seen
        assert delivered == {"ogm", "itfaiye", "muhtarlik"}

        assert httpx.post(base + "/api/command", json={"target": 1, "cmd": "silence"}, timeout=5).json()["ok"]
        acked = False
        t_end = time.monotonic() + 5
        while time.monotonic() < t_end and not acked:
            ev = _recv(ws)
            acked = bool(ev and ev["type"] == "telemetry" and ev["node"]["id"] == "F1" and ev["node"]["flags"] & 4)
        assert acked, "F1 susturma onayı (flags&4) gelmedi"

    state = httpx.get(base + "/api/state").json()
    assert state["incidents"][0]["silenced_at"] is not None


def test_ui_is_served_without_cdn(server):
    base, _ = server
    html = httpx.get(base + "/").text
    assert "vendor/maplibre-gl/maplibre-gl.js" in html
    for cdn in ("unpkg", "jsdelivr", "cdnjs", "googleapis", "tailwind"):
        assert cdn not in html
    for path in ("/style.css", "/js/app.js", "/js/map.js", "/js/ui.js", "/js/ws.js",
                 "/vendor/maplibre-gl/maplibre-gl.js", "/vendor/maplibre-gl/maplibre-gl.css"):
        assert httpx.get(base + path).status_code == 200, path
    assert httpx.get(base + "/tiles/yok/1/0/0").status_code == 404
    assert httpx.get(base + "/tiles/dem/99/0/0").status_code == 404
