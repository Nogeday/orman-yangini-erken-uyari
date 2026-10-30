#!/usr/bin/env python3
"""MapLibre GL JS 5.24.0 (UMD) dağıtım dosyalarını npm paketinden indirip
simulation/web/vendor/maplibre-gl/ altına koyar. Çalışma anında CDN kullanılmaz;
bu dosyalar repoya commit edilir. Yalnızca sürüm yükseltirken yeniden çalıştırın.

Kullanım:  python simulation/tools/vendor_maplibre.py
"""
from __future__ import annotations

import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "5.24.0"
URL = f"https://registry.npmjs.org/maplibre-gl/-/maplibre-gl-{VERSION}.tgz"
# npm kayıt defterindeki "integrity" değeri (dist.integrity) — indirilen paket bununla doğrulanır.
INTEGRITY = "sha512-ALyFxgtd5R+65UqZ/++lOqwWcC0SNho9c27fYSyLmG7AfnAul2o46F05aDJGPbFU57wos9dgcIySHs0Xe6ia3A=="
WANTED = {
    "package/dist/maplibre-gl.js": "maplibre-gl.js",
    "package/dist/maplibre-gl.css": "maplibre-gl.css",
    "package/LICENSE.txt": "LICENSE.txt",
}
OUT = Path(__file__).resolve().parents[1] / "web" / "vendor" / "maplibre-gl"


def main() -> int:
    print(f"İndiriliyor: {URL}")
    req = urllib.request.Request(URL, headers={"User-Agent": "orman-yangin-mesh/1.0 (hackathon demo)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        blob = resp.read()
    digest = "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()
    if digest != INTEGRITY:
        print(f"HATA: paket bütünlüğü uyuşmuyor\n  beklenen {INTEGRITY}\n  gelen    {digest}")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    found = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name == "package/dist/LICENSE.txt":
                name = "package/LICENSE.txt"
            if name in WANTED and member.isfile():
                f = tar.extractfile(member)
                if f is not None:
                    found[WANTED[name]] = f.read()
    missing = sorted(set(WANTED.values()) - set(found))
    if missing:
        print(f"HATA: pakette bulunamadı: {', '.join(missing)}")
        return 1
    for fname, data in found.items():
        (OUT / fname).write_bytes(data)
        print(f"  {fname:18s} {len(data) / 1024:8.1f} KB")
    (OUT / "VERSION").write_text(f"maplibre-gl {VERSION}\n{URL}\n{INTEGRITY}\n", encoding="utf-8")
    print(f"Tamam → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
