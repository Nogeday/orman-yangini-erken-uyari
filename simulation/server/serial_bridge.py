"""USB seri köprü — Linux ve Windows için tek kod (pyserial).

- Port bulma: önce bilinen USB-UART VID:PID'ler, sonra ad kalıpları (/dev/ttyUSB*, /dev/ttyACM*, COM*).
- Port `dtr=False, rts=False` ile açılır (ESP32 yeniden başlamasın), `status` yollanır; 2.5 s içinde
  `hello`/`tel` gelirse kabul edilir. `hello.role` köprüyü (gateway) ve doğrudan takılı F1'i (sensor,
  B planı) ayırır. Birden çok port aynı anda okunabilir (köprü + USB'deki F1).
- Okuma ayrı iş parçacığında; her satır alındığı anda geri çağrıyla verilir (toplama yok).
- Kopma: 2 s'de bir yeniden dene. Tarama ayrı iş parçacığında, okumayı bekletmez.
"""
from __future__ import annotations

import errno
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import serial
from serial.tools import list_ports

from .protocol import Hello, Telemetry, parse_line

KNOWN_USB_UART = {
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x0403, 0x6001): "FTDI",
}
NAME_PREFIXES = ("/dev/ttyUSB", "/dev/ttyACM", "/dev/cu.usbserial", "/dev/cu.SLAB", "/dev/cu.wchusbserial")
PROBE_TIMEOUT_S = 2.5
RETRY_S = 2.0
SCAN_S = 3.0
FAILED_PORT_BACKOFF_S = 15.0

LineCallback = Callable[[str, float, str], None]     # (satır, alındığı an [time.time()], port)
StatusCallback = Callable[[dict], None]


@dataclass(frozen=True)
class PortInfo:
    device: str
    chip: str
    description: str


def candidate_ports() -> list[PortInfo]:
    found = []
    for p in list_ports.comports():
        chip = KNOWN_USB_UART.get((p.vid, p.pid)) if p.vid is not None else None
        dev = p.device
        name_ok = dev.startswith(NAME_PREFIXES) or dev.upper().startswith("COM")
        if chip or name_ok:
            found.append((0 if chip else 1, dev, chip or "", p.description or ""))
    found.sort()
    return [PortInfo(dev, chip, desc) for _, dev, chip, desc in found]


def open_port(name: str, baud: int) -> serial.Serial:
    s = serial.Serial()
    s.port = name
    s.baudrate = baud
    s.timeout = 0
    s.write_timeout = 1.0
    s.dtr = False      # ESP32'yi yeniden başlatma / boot moduna sokma
    s.rts = False
    s.open()
    return s


def explain_error(exc: BaseException, port: str) -> str:
    text = str(exc)
    err = getattr(exc, "errno", None)
    denied = isinstance(exc, PermissionError) or err == errno.EACCES or "Permission denied" in text \
        or "Access is denied" in text or "PermissionError" in text
    if denied:
        if os.name == "nt":
            return f"{port} meşgul ya da erişim reddedildi: Arduino IDE / seri monitörü (pio device monitor) kapatın."
        return f"{port}: izin yok → sudo usermod -aG dialout $USER, sonra oturumu kapatıp açın."
    if err == errno.EBUSY or "busy" in text.lower():
        return f"{port} meşgul: başka bir program (seri monitör) portu kullanıyor; kapatın."
    if isinstance(exc, FileNotFoundError) or "FileNotFoundError" in text or "No such file" in text:
        return f"{port} bulunamadı (kart çıkarıldı mı?)."
    return f"{port} açılamadı: {text}"


def no_port_hint() -> str:
    if os.name == "nt":
        return ("Donanım bulunamadı: COM portu yok. Kart takılı mı? Sürücü gerekebilir: "
                "CH340 (WCH) ya da CP210x (Silicon Labs).")
    if sys.platform == "darwin":
        return "Donanım bulunamadı: /dev/cu.usbserial* yok. CH340 / CP210x sürücüsünü kurun."
    return ("Donanım bulunamadı: /dev/ttyUSB* / ttyACM* yok. Kart takılı mı? "
            "Ubuntu'da görünmüyorsa: sudo apt remove brltty (CH340'ı kapıyor).")


@dataclass
class _Conn:
    port: str
    ser: serial.Serial
    role: str
    hello: Optional[dict]
    buf: bytearray = field(default_factory=bytearray)


@dataclass
class ProbeResult:
    conn: _Conn
    lines: list[tuple[str, float]]


def probe(port: str, baud: int, timeout_s: float = PROBE_TIMEOUT_S) -> Optional[ProbeResult]:
    """Portu açar, `status` yollar; hello/tel gelirse açık bağlantıyı döner, gelmezse kapatır."""
    ser = open_port(port, baud)
    lines: list[tuple[str, float]] = []
    role: Optional[str] = None
    hello: Optional[dict] = None
    buf = bytearray()
    try:
        ser.write(b"status\n")
        deadline = time.monotonic() + timeout_s
        settle: Optional[float] = None
        while time.monotonic() < deadline:
            n = ser.in_waiting
            chunk = ser.read(n) if n else b""
            if not chunk:
                if settle is not None and time.monotonic() >= settle:
                    break
                time.sleep(0.01)
                continue
            now = time.time()
            buf += chunk
            while b"\n" in buf:
                raw, _, rest = bytes(buf).partition(b"\n")
                buf = bytearray(rest)
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                lines.append((text, now))
                msg = parse_line(text)
                if isinstance(msg, Hello):
                    role = msg.role
                    hello = {"role": msg.role, "fw": msg.fw, "node": msg.node}
                    settle = time.monotonic() + 0.2      # ardından gelen gw satırını da al
                elif isinstance(msg, Telemetry) and role is None:
                    role = "gateway" if msg.via in ("rf", "local") else "sensor"
                    settle = time.monotonic() + 0.3
    except Exception:
        ser.close()
        raise
    if role is None:
        ser.close()
        return None
    return ProbeResult(_Conn(port, ser, role, hello, buf), lines)


class SerialBridge:
    def __init__(self, on_line: LineCallback, on_status: StatusCallback, port: Optional[str] = None,
                 baud: int = 115200, multi: bool = True) -> None:
        self.on_line = on_line
        self.on_status = on_status
        self.port_arg = port
        self.baud = baud
        self.multi = multi
        self._conns: dict[str, _Conn] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._failed: dict[str, float] = {}
        self._ever_connected = False
        self._last_status: Optional[tuple] = None
        self._threads: list[threading.Thread] = []

    # ---- dış arayüz ----
    def start(self) -> None:
        for name, target in (("seri-tarama", self._scan_loop), ("seri-okuma", self._read_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.5)
        with self._lock:
            for c in self._conns.values():
                try:
                    c.ser.close()
                except Exception:  # noqa: BLE001
                    pass
            self._conns.clear()

    @property
    def connected(self) -> bool:
        with self._lock:
            return bool(self._conns)

    def ports(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(c.port, c.role) for c in self._conns.values()]

    def send(self, cmd: str) -> bool:
        """Komutu köprüye (gateway) yazar. F1 firmware'i seri komut almaz."""
        with self._lock:
            conn = next((c for c in self._conns.values() if c.role == "gateway"), None)
            if conn is None:
                return False
            try:
                conn.ser.write((cmd.strip() + "\n").encode("ascii", "replace"))
                conn.ser.flush()
                return True
            except (serial.SerialException, OSError):
                return False

    # ---- iç ----
    def _status(self, state: str, message: str) -> None:
        ports = self.ports()
        key = (state, tuple(ports), message)
        if key == self._last_status:
            return
        self._last_status = key
        gateway = next((p for p, r in ports if r == "gateway"), None)
        self.on_status({"state": state, "ports": ports, "gateway": gateway, "message": message})

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                have = set(self._conns)
                have_gateway = any(c.role == "gateway" for c in self._conns.values())
            if not have or (self.multi and self.port_arg is None):
                self._scan_once(have, have_gateway)
            self._stop.wait(RETRY_S if self._ever_connected and not have else SCAN_S)

    def _scan_once(self, have: set[str], have_gateway: bool) -> None:
        ports = [PortInfo(self.port_arg, "", "")] if self.port_arg else candidate_ports()
        if not ports and not have:
            self._status("searching" if not self._ever_connected else "lost",
                         no_port_hint() if not self._ever_connected
                         else "Donanım bağlantısı koptu — yeniden deneniyor")
            return
        errors = []
        now = time.monotonic()
        for p in ports:
            if p.device in have or self._failed.get(p.device, 0) > now:
                continue
            try:
                res = probe(p.device, self.baud)
            except (serial.SerialException, OSError, ValueError) as exc:
                errors.append(explain_error(exc, p.device))
                if have:
                    self._failed[p.device] = now + FAILED_PORT_BACKOFF_S
                continue
            if res is None:
                errors.append(f"{p.device}: yanıt yok (firmware yüklü mü? baud {self.baud})")
                if have:
                    self._failed[p.device] = now + FAILED_PORT_BACKOFF_S
                continue
            with self._lock:
                self._conns[p.device] = res.conn
            self._ever_connected = True
            for text, ts in res.lines:
                self.on_line(text, ts, p.device)
            role = "köprü F2" if res.conn.role == "gateway" else "F1 doğrudan USB (B planı)"
            chip = f", {p.chip}" if p.chip else ""
            self._status("connected", f"Donanım bağlandı: {p.device} ({role}{chip})")
            have.add(p.device)
            if not self.multi:
                break
        if not have:
            msg = errors[0] if errors else no_port_hint()
            if self._ever_connected:
                msg = "Donanım bağlantısı koptu — yeniden deneniyor" + (f" ({errors[0]})" if errors else "")
            self._status("lost" if self._ever_connected else "searching", msg)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                conns = list(self._conns.values())
            if not conns:
                self._stop.wait(0.05)
                continue
            got = False
            for c in conns:
                try:
                    n = c.ser.in_waiting
                    chunk = c.ser.read(n) if n else b""
                except (serial.SerialException, OSError, TypeError, AttributeError):
                    self._drop(c)
                    continue
                if not chunk:
                    continue
                got = True
                now = time.time()
                c.buf += chunk
                while True:
                    i = c.buf.find(b"\n")
                    if i < 0:
                        break
                    raw = bytes(c.buf[:i])
                    del c.buf[:i + 1]
                    text = raw.decode("utf-8", "replace").strip()
                    if text:
                        self.on_line(text, now, c.port)
                if len(c.buf) > 4096:       # satır sonu gelmeyen çöp
                    c.buf.clear()
            if not got:
                time.sleep(0.002)

    def _drop(self, c: _Conn) -> None:
        with self._lock:
            self._conns.pop(c.port, None)
        try:
            c.ser.close()
        except Exception:  # noqa: BLE001
            pass
        if self.connected:
            self._status("connected", f"{c.port} çıkarıldı; diğer port(lar) okunuyor")
        else:
            self._status("lost", f"Donanım bağlantısı koptu — yeniden deneniyor ({c.port})")
