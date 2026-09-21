"""
Phase 0 probe #41: the item model records around the owners probe_names25 found.

Two owners hold the composed names as FStrings next to per-item data:
- records at 0x2d0baa050 (Vladof AR) and 0x2d0baa398 (Jakobs shotgun), 840 bytes apart,
  with serial (@Ug...), rarity, slot, name, thumbnail path, element and damage text;
- records at 0x141584f0 / 0x14158c28 with slug fields (Vladof-Assault-Rifle, legendary,
  vladof, weapon, assaultrifle, 546.213).
This probe dumps both regions and decodes every FString, every known item seed (the ids
the mod stores) and every double/int in a readable layout, so the record structure and the
key that ties a record to an item become visible.

Raw reads go through ReadProcessMemory. No hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import struct
import time
from pathlib import Path

from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MAX_TEXT_CHARS = 160
REGIONS = (
    ("item models", 0x2D0BAA050 - 1600, 0x2D0BAA398 + 1400),
    ("slug records", 0x141584F0 - 400, 0x14158C28 + 1900),
)
KNOWN_SEEDS = {
    "ddbe5d456a2ec99a4c917f15": "Jakobs Shotgun",
    "9bf02ca566b1c0010e1227cd": "Vladof AR",
    "134844c299550a25ddaa1fe6": "Vladof SMG",
    "4403462d66781a89a0eccea0": "Torgue Pistol",
}

kernel32 = ctypes.windll.kernel32
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
kernel32.ReadProcessMemory.restype = wintypes.BOOL
PROCESS = kernel32.GetCurrentProcess()


def say(msg: str) -> None:
    logging.info(f"[dmgnames26] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def read(address: int, size: int) -> bytes | None:
    if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(PROCESS, ctypes.c_void_p(address), buffer, size, ctypes.byref(got))
    if not ok and got.value == 0:
        return None
    return buffer.raw[:got.value]


def u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def i32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def is_pointer(word: int) -> bool:
    return POINTER_MIN <= word <= POINTER_MAX


def utf16_at(address: int, limit: int) -> str | None:
    raw = read(address, limit * 2)
    if raw is None or len(raw) < 2:
        return None
    end = raw.find(b"\x00\x00")
    if end % 2:
        end += 1
    chunk = raw[:end] if end >= 0 else raw
    try:
        text = chunk.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not all(32 <= ord(ch) < 0x3000 for ch in text):
        return None
    return text


def fstring_text(data: bytes, offset: int) -> str | None:
    if offset + 16 > len(data):
        return None
    pointer, num, capacity = u64(data, offset), i32(data, offset + 8), i32(data, offset + 12)
    if not is_pointer(pointer) or num < 1 or num > 2048 or capacity < num or capacity > 8192:
        return None
    if num == 1:
        return ""
    return utf16_at(pointer, min(num, MAX_TEXT_CHARS))


def dump_region(label: str, start: int, end: int) -> None:
    data = read(start, end - start)
    if data is None:
        say(f"   {label}: unreadable")
        return
    say(f"   {label}: {start:#x}..{end:#x} ({len(data)} bytes)")
    seeds = {bytes.fromhex(seed): name for seed, name in KNOWN_SEEDS.items()}
    for seed, name in seeds.items():
        position = data.find(seed)
        while position >= 0:
            say(f"      {start + position:#x} (+{position}): seed of {name}")
            position = data.find(seed, position + 1)
    offset = 0
    while offset + 8 <= len(data):
        text = fstring_text(data, offset)
        if text is not None:
            say(f"      {start + offset:#x} (+{offset}): FString {text!r}")
            offset += 16
            continue
        word = u64(data, offset)
        if word == 0:
            offset += 8
            continue
        as_double = struct.unpack("<d", data[offset:offset + 8])[0]
        low, high = i32(data, offset), i32(data, offset + 4)
        note = f"{word:#x}"
        if 1e-6 < abs(as_double) < 1e9:
            note += f" double {as_double:.6g}"
        if -100000 < low < 100000 and -100000 < high < 100000:
            note += f" ints ({low}, {high})"
        say(f"      {start + offset:#x} (+{offset}): {note}")
        offset += 8


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v26: the item model records")
try:
    for label, start, end in REGIONS:
        dump_region(label, start, end)
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
say("done")
