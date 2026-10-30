#!/usr/bin/env python3
"""
Köprü (F2) ya da F1 seri çıktısını okunur biçimde gösteren donanım test aracı.
Linux (/dev/ttyUSB*, /dev/ttyACM*) ve Windows (COM*) üzerinde çalışır.

Kurulum :  pip install pyserial
Kullanım :  python seri_test.py                  # portu otomatik bulur
            python seri_test.py --port COM5      # Windows
            python seri_test.py --port /dev/ttyUSB0 --send "test 1"
            python seri_test.py --list           # portları listele
Çıkış    :  Ctrl+C
"""
from __future__ import annotations

import argparse
import json
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    sys.exit("pyserial eksik:  pip install pyserial")

# ESP32 kartlarında yaygın USB-UART çipleri (VID, PID)
KNOWN_USB_UART = {
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x0403, 0x6001): "FT232",
    (0x303A, 0x1001): "ESP32 USB-JTAG",
}
ALARM_TEXT = {0: "NORMAL", 1: "GAZ", 2: "ISI", 3: "YANGIN"}


def candidate_ports():
    found = []
    for p in list_ports.comports():
        chip = KNOWN_USB_UART.get((p.vid, p.pid)) if p.vid is not None else None
        name = p.device
        looks_usb = name.startswith(("/dev/ttyUSB", "/dev/ttyACM", "/dev/cu.usb")) or name.upper().startswith("COM")
        if chip or looks_usb:
            found.append((0 if chip else 1, name, chip or (p.description or "?")))
    found.sort()
    return [(n, d) for _, n, d in found]


def open_port(name: str, baud: int) -> serial.Serial:
    s = serial.Serial()
    s.port = name
    s.baudrate = baud
    s.timeout = 0.2
    s.dtr = False   # ESP32'yi yeniden başlatma / boot moduna sokma
    s.rts = False
    s.open()
    return s


def flags_text(f: int) -> str:
    parts = []
    if f & 1:
        parts.append("ısınıyor")
    if f & 2:
        parts.append("DHT-hata")
    if f & 4:
        parts.append("susturuldu")
    if f & 8:
        parts.append("TEST")
    return ",".join(parts) or "-"


def show(obj: dict) -> str:
    t = obj.get("type")
    if t == "tel":
        temp = "—" if obj.get("t") is None else f"{obj['t']:.1f}°C"
        hum = "—" if obj.get("h") is None else f"%{obj['h']:.0f}"
        delta = obj.get("gas", 0) - obj.get("base", 0)
        extra = f" lq=%{obj['lq']}" if "lq" in obj else ""
        extra += f" rf_ok={obj['rf_ok']}" if "rf_ok" in obj else ""
        return (f"F{obj.get('node')} [{obj.get('via')}/{obj.get('kind')}] #{obj.get('seq')}  "
                f"{temp} {hum}  gaz={obj.get('gas')}mV (Δ{delta:+d})  "
                f"{ALARM_TEXT.get(obj.get('alarm', 0), '?')}  bayrak={flags_text(obj.get('flags', 0))}{extra}")
    if t == "link":
        return f"BAĞLANTI F{obj.get('node')}: {obj.get('state', '').upper()} ({obj.get('age_ms')} ms)"
    if t == "gw":
        return (f"KÖPRÜ durum: radyo={obj.get('radio')} kanal={obj.get('ch')} alınan={obj.get('rx')} "
                f"kayıp={obj.get('lost')} lq={obj.get('lq')} F1={obj.get('n1_link')}")
    return json.dumps(obj, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Orman mesh köprüsü seri test aracı")
    ap.add_argument("--port", help="Seri port (ör. /dev/ttyUSB0 veya COM5). Boşsa otomatik.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--send", action="append", default=[], help="Açılışta gönderilecek komut (tekrarlanabilir)")
    ap.add_argument("--raw", action="store_true", help="Satırları ham JSON olarak yaz")
    ap.add_argument("--list", action="store_true", help="Portları listele ve çık")
    args = ap.parse_args()

    ports = candidate_ports()
    if args.list:
        for name, desc in ports:
            print(f"{name:20s} {desc}")
        return 0
    port = args.port or (ports[0][0] if ports else None)
    if not port:
        print("Seri port bulunamadı. Kart takılı mı? Linux: 'ls /dev/ttyUSB* /dev/ttyACM*'  Windows: Aygıt Yöneticisi → COM")
        return 1
    try:
        ser = open_port(port, args.baud)
    except PermissionError:
        print(f"{port}: izin yok → sudo usermod -aG dialout $USER  (sonra oturumu kapatıp açın)")
        return 1
    except serial.SerialException as exc:
        print(f"{port} açılamadı: {exc}\nPort başka bir programda açık olabilir (Arduino IDE / pio monitor).")
        return 1

    print(f"✓ {port} @ {args.baud} açık. Ctrl+C ile çıkın.")
    time.sleep(0.3)
    for cmd in (["status"] + args.send):
        ser.write((cmd.strip() + "\n").encode())
    buf = b""
    try:
        while True:
            chunk = ser.read(512)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue   # ESP32 açılış mesajları vb.
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                stamp = time.strftime("%H:%M:%S")
                print(f"{stamp}  {text if args.raw else show(obj)}")
    except KeyboardInterrupt:
        print("\nçıkılıyor")
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
