#!/usr/bin/env python3
"""Tek komut:  Linux  python3 simulation/run.py      Windows  py simulation\\run.py

İlk çalıştırmada simulation/.venv oluşturur ve bağımlılıkları kurar (internet gerekir),
sonra kendini bu sanal ortamda yeniden başlatır, sunucuyu açar ve tarayıcıyı açar.

  --source auto|serial|demo|replay   veri kaynağı (varsayılan auto: donanım yoksa DEMO)
  --port /dev/ttyUSB0 | COM5         seri portu elle seç (boşsa otomatik bulunur)
  --baud 115200  --http-port 8000  --host 127.0.0.1  --no-browser
  --replay-file docs/ornek_seri_kayit.tsv  [--loop]   --area simulation/config/area.json   --no-log
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parent
VENV_DIR = SIM_DIR / ".venv"
REQUIREMENTS = SIM_DIR / "requirements.txt"
STAMP = VENV_DIR / ".requirements.sha256"
MIN_PY = (3, 10)


def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_project_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def _requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def ensure_venv() -> Path:
    if sys.version_info < MIN_PY:
        sys.exit(f"Python {MIN_PY[0]}.{MIN_PY[1]} veya üstü gerekli (şu an {sys.version.split()[0]}). "
                 "https://www.python.org/downloads/")
    py = venv_python()
    if not py.exists():
        print("İlk çalıştırma: sanal ortam oluşturuluyor → simulation/.venv")
        try:
            import venv
            venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        except Exception as exc:  # noqa: BLE001
            hint = "Ubuntu/Debian: sudo apt install python3-venv" if os.name != "nt" else \
                   "Python'u python.org'dan 'pip' seçeneğiyle yeniden kurun."
            sys.exit(f"Sanal ortam oluşturulamadı: {exc}\n{hint}")
    if not STAMP.exists() or STAMP.read_text(encoding="utf-8").strip() != _requirements_hash():
        print("Bağımlılıklar kuruluyor (yalnızca ilk sefer; internet gerekir)…")
        rc = subprocess.call([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                              "-r", str(REQUIREMENTS)])
        if rc != 0:
            sys.exit("Bağımlılık kurulumu başarısız. İnternet bağlantısını kontrol edip tekrar deneyin.")
        STAMP.write_text(_requirements_hash(), encoding="utf-8")
    return py


def reexec_in_venv() -> None:
    """Geçerli betiği proje sanal ortamındaki Python ile yeniden çalıştırır."""
    py = ensure_venv()
    args = [str(py), os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    if os.name == "nt":
        try:
            sys.exit(subprocess.call(args))
        except KeyboardInterrupt:
            sys.exit(130)
    os.execv(str(py), args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Orman Yangını Erken Uyarı Ağı — simülasyon sunucusu")
    ap.add_argument("--source", choices=["auto", "serial", "demo", "replay"], default="auto",
                    help="veri kaynağı (varsayılan: auto)")
    ap.add_argument("--port", help="seri port, ör. /dev/ttyUSB0 veya COM5 (boşsa otomatik)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="tarayıcıyı açma")
    ap.add_argument("--replay-file", type=Path, help="replay kaydı (.tsv ya da oturum .jsonl)")
    ap.add_argument("--loop", action="store_true", help="replay kaydı bitince başa sar")
    ap.add_argument("--area", type=Path, help="bölge dosyası (varsayılan simulation/config/area.json)")
    ap.add_argument("--no-log", action="store_true", help="ham satırları simulation/data/logs altına yazma")
    return ap.parse_args(argv)


def main() -> None:
    args = parse_args()
    if not in_project_venv() and not os.environ.get("ORMAN_NO_VENV"):
        reexec_in_venv()
    sys.path.insert(0, str(SIM_DIR))
    from server.config import DEFAULT_AREA, DEFAULT_REPLAY, Settings
    from server.main import serve

    replay = args.replay_file
    if args.source == "replay" and replay is None:
        replay = DEFAULT_REPLAY
    serve(Settings(
        source=args.source, port=args.port, baud=args.baud, http_port=args.http_port, host=args.host,
        no_browser=args.no_browser, replay_file=replay, replay_loop=args.loop,
        area=(args.area or DEFAULT_AREA).resolve(), session_log=not args.no_log,
    ))


if __name__ == "__main__":
    main()
