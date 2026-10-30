"""Seri protokol: ayrıştırma, tekrar eleme ve firmware ile birebir aynı satır biçimi.

Tek doğruluk kaynağı docs/PROTOCOL.md §2'dir. Gerçek seri port, demo ve replay satırları
aynı `parse_line` fonksiyonundan geçer.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

ALARM_GAS = 0x01
ALARM_HEAT = 0x02
ALARM_NAMES = {0: "NORMAL", 1: "GAZ", 2: "ISI", 3: "YANGIN"}

FLAG_WARMUP = 0x01
FLAG_DHT_ERR = 0x02
FLAG_SILENCED = 0x04
FLAG_TEST = 0x08


class _Msg(BaseModel):
    # Firmware'e ileride eklenecek alanlar ayrıştırıcıyı bozmasın.
    model_config = ConfigDict(extra="allow")


class Hello(_Msg):
    type: Literal["hello"]
    role: str
    node: int
    fw: str = ""
    proto: int = 1
    radio: int = 0
    ch: int = 0


class Telemetry(_Msg):
    type: Literal["tel"]
    node: int
    via: Literal["rf", "local", "usb"]
    kind: Literal["periodic", "event", "boot"] = "periodic"
    seq: int
    up: int
    t: Optional[float] = None
    h: Optional[float] = None
    gas: int
    base: int
    alarm: int = Field(ge=0, le=3)
    flags: int = 0
    vbat: int = 0
    txf: int = 0
    lq: Optional[int] = None
    rf_ok: Optional[int] = None


class Link(_Msg):
    type: Literal["link"]
    node: int
    state: Literal["lost", "ok"]
    age_ms: int = 0


class GatewayStatus(_Msg):
    type: Literal["gw"]
    node: int = 2
    up: int = 0
    radio: int = 0
    ch: int = 0
    rx: int = 0
    lost: int = 0
    lq: int = -1
    n1_link: str = "never"
    n1_age_ms: int = -1


class CommandResult(_Msg):
    type: Literal["cmd"]
    ok: bool
    cmd: str = ""
    target: str = ""
    note: str = ""


class Pong(_Msg):
    type: Literal["pong"]
    node: int = 0
    up: int = 0


class Help(_Msg):
    type: Literal["help"]
    cmds: str = ""


class DeviceLog(_Msg):
    type: Literal["log"]
    node: int = 0
    level: str = "info"
    msg: str = ""


Message = Annotated[
    Union[Hello, Telemetry, Link, GatewayStatus, CommandResult, Pong, Help, DeviceLog],
    Field(discriminator="type"),
]
_message_adapter: TypeAdapter[Message] = TypeAdapter(Message)


def parse_line(line: str | bytes) -> Optional[Message]:
    """Tek seri satırı mesaja çevirir. `{` ile başlamayan, JSON olmayan ya da şemaya
    uymayan satırlar için None döner (ESP32 açılışta ROM mesajları basar)."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    try:
        return _message_adapter.validate_python(obj)
    except ValidationError:
        return None


class Deduper:
    """`(node, up, seq)` üçlüsüyle tekrar eden `tel` satırlarını eler (PROTOCOL.md §2).

    F1 hem radyodan (köprü) hem doğrudan USB'den gelebilir; ikinci kopya atılır.
    Anahtarlar `window_s` saniye sonra unutulur: düğüm yeniden başlarsa (up=0, seq=0)
    ya da kayıt baştan oynatılırsa yeni paketler yanlışlıkla elenmez.
    """

    def __init__(self, window_s: float = 10.0, max_keys: int = 4096) -> None:
        self.window_s = window_s
        self.max_keys = max_keys
        self._seen: OrderedDict[tuple[int, int, int], float] = OrderedDict()

    def is_duplicate(self, msg: Telemetry, now: float) -> bool:
        while self._seen:
            _, first_at = next(iter(self._seen.items()))
            if now - first_at > self.window_s or len(self._seen) > self.max_keys:
                self._seen.popitem(last=False)
            else:
                break
        key = (msg.node, msg.up, msg.seq)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def clear(self) -> None:
        self._seen.clear()


def alarm_name(alarm: int) -> str:
    return ALARM_NAMES.get(alarm, "?")


def flags_text(flags: int) -> list[str]:
    parts = []
    if flags & FLAG_WARMUP:
        parts.append("ISINIYOR")
    if flags & FLAG_DHT_ERR:
        parts.append("DHT HATASI")
    if flags & FLAG_SILENCED:
        parts.append("SUSTURULDU")
    if flags & FLAG_TEST:
        parts.append("TEST")
    return parts


# ---------------------------------------------------------------------------
#  Firmware ile birebir aynı satır biçimi (node*/src/main.cpp → printPacketJson vb.)
#  Demo modundaki sanal düğümler bu fonksiyonlarla satır üretir.
# ---------------------------------------------------------------------------
TEMP_INVALID = -32768
HUM_INVALID = 0xFFFF
KIND_NAMES = {0: "periodic", 1: "event", 2: "boot"}


def _fmt_x10(value: int, valid: bool) -> str:
    if not valid:
        return "null"
    neg = value < 0
    a = -value if neg else value
    return f"{'-' if neg else ''}{a // 10}.{a % 10}"


def format_tel(
    *,
    node: int,
    via: str,
    ptype: int,
    seq: int,
    up: int,
    temp_x10: int,
    hum_x10: int,
    gas: int,
    base: int,
    alarm: int,
    flags: int,
    vbat: int = 0,
    txf: int = 0,
    lq: int = -1,
    rf_ok: int = -1,
) -> str:
    t = _fmt_x10(temp_x10, temp_x10 != TEMP_INVALID)
    h = _fmt_x10(hum_x10, hum_x10 != HUM_INVALID)
    line = (
        f'{{"type":"tel","node":{node},"via":"{via}","kind":"{KIND_NAMES.get(ptype, "periodic")}",'
        f'"seq":{seq},"up":{up},"t":{t},"h":{h},"gas":{gas},"base":{base},"alarm":{alarm},'
        f'"flags":{flags},"vbat":{vbat},"txf":{txf}'
    )
    if lq >= 0:
        line += f',"lq":{lq}'
    if rf_ok >= 0:
        line += f',"rf_ok":{rf_ok}'
    return line + "}"


def format_hello(role: str, node: int, radio: int = 1, ch: int = 78, fw: str = "1.0.0", proto: int = 1) -> str:
    return (
        f'{{"type":"hello","role":"{role}","node":{node},"fw":"{fw}","proto":{proto},'
        f'"radio":{radio},"ch":{ch}}}'
    )


def format_link(node: int, state: str, age_ms: int) -> str:
    return f'{{"type":"link","node":{node},"state":"{state}","age_ms":{age_ms}}}'


def format_gw(*, up: int, rx: int, lost: int, lq: int, n1_link: str, n1_age_ms: int, radio: int = 1, ch: int = 78) -> str:
    return (
        f'{{"type":"gw","node":2,"up":{up},"radio":{radio},"ch":{ch},"rx":{rx},"lost":{lost},'
        f'"lq":{lq},"n1_link":"{n1_link}","n1_age_ms":{n1_age_ms}}}'
    )


def format_cmd(ok: bool, cmd: str, target: str, note: str) -> str:
    return (
        f'{{"type":"cmd","ok":{"true" if ok else "false"},"cmd":"{cmd}","target":"{target}",'
        f'"note":"{note}"}}'
    )
