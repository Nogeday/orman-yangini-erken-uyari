"""Seri köprü, sahte bir USB-seri cihaz (pseudo-terminal) üzerinde: port yoklama (status → hello),
ESP32 açılış çöpünü atlama, satırları anında iletme ve komut yazma. Yalnızca POSIX."""
import os
import sys
import threading
import time

import pytest

from server.serial_bridge import SerialBridge, explain_error

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="pseudo-terminal yalnızca POSIX")

HELLO = b'{"type":"hello","role":"gateway","node":2,"fw":"1.0.0","proto":1,"radio":1,"ch":78}\n'
ALARM = (b'{"type":"tel","node":1,"via":"rf","kind":"event","seq":61,"up":60300,"t":24.0,"h":45.0,"gas":776,'
         b'"base":324,"alarm":1,"flags":0,"vbat":0,"txf":0,"lq":100}\n')


def _wait(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_bridge_probes_reads_and_writes_over_pty():
    import pty
    import select
    import tty

    master, slave = pty.openpty()
    tty.setraw(slave)
    port = os.ttyname(slave)
    lines, statuses, received = [], [], bytearray()
    stop = threading.Event()

    def device():                       # köprü (F2) taklidi: "status" gelince hello basar
        while not stop.is_set():
            r, _, _ = select.select([master], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(master, 1024)
            except OSError:
                return
            received.extend(data)
            if b"status\n" in data:
                os.write(master, HELLO)

    threading.Thread(target=device, daemon=True).start()
    bridge = SerialBridge(lambda text, ts, p: lines.append((text, ts, p)), statuses.append, port=port, multi=False)
    bridge.start()
    try:
        assert _wait(lambda: any(s["state"] == "connected" for s in statuses)), statuses
        conn = next(s for s in statuses if s["state"] == "connected")
        assert conn["gateway"] == port and "köprü F2" in conn["message"]
        assert any('"type":"hello"' in t for t, _, _ in lines)

        os.write(master, b"ets Jun  8 2016 00:22:57\r\n" + ALARM)
        t_sent = time.time()
        assert _wait(lambda: any('"alarm":1' in t for t, _, _ in lines))
        text, ts, p = next(x for x in lines if '"alarm":1' in x[0])
        assert p == port and ts - t_sent < 0.15                      # satır anında iletilir
        assert any(t.startswith("ets Jun") for t, _, _ in lines)     # ham satır kayda gider, ayrıştırıcı atlar

        assert bridge.send("silence 1")
        assert _wait(lambda: b"silence 1\n" in received)
    finally:
        bridge.stop()
        stop.set()
        os.close(master)
        os.close(slave)


def test_error_messages_are_actionable():
    msg = explain_error(PermissionError(13, "Permission denied"), "/dev/ttyUSB0" if os.name != "nt" else "COM5")
    assert ("dialout" in msg) if os.name != "nt" else ("seri monitörü" in msg)
    assert "çıkarıldı" in explain_error(FileNotFoundError(2, "No such file"), "/dev/ttyUSB0")
