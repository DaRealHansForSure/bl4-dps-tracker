"""
Phase 0 probe #42: what owns the equipped-item model block?

probe_names26 mapped the block at 0x2d0ba9a10..: five 288-byte slot records (type slug,
rarity slug, a full identity copy with the seed id), then four 280-byte item detail records
(slot type, display name, thumbnail, element, damage text, serial, rarity), then a list of
empty 40-byte entries. That block gives every equipped weapon's name keyed by its seed. To
reach it at runtime the mod needs its owner. This probe:

1. dumps the words before the block to find its start (vtable / UObject header / array);
2. looks for pointers into the block inside the UI data collectors, the player controller,
   the HUD, the local player, the game instance and the pawn (their own bytes and one level
   of pointers), which is instant;
3. lists which UObjects, by class, sit within 64 KB of the block (UObjects nearby often
   own nearby allocations).

Raw reads go through ReadProcessMemory. No hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
BLOCK_START = 0x2D0BA9A10
BLOCK_END = 0x2D0BAA900
BEFORE_BYTES = 1024
NEAR_BYTES = 64 * 1024
MAX_TEXT_CHARS = 120

kernel32 = ctypes.windll.kernel32
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
kernel32.ReadProcessMemory.restype = wintypes.BOOL
PROCESS = kernel32.GetCurrentProcess()


def say(msg: str) -> None:
    logging.info(f"[dmgnames27] {msg}")
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


def hex_words(data: bytes) -> str:
    return " ".join(data[i:i + 8].hex() for i in range(0, len(data) - 7, 8))


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
    return "" if num == 1 else utf16_at(pointer, min(num, MAX_TEXT_CHARS))


def dump_before() -> None:
    say("step 1: the words before the block")
    data = read(BLOCK_START - BEFORE_BYTES, BEFORE_BYTES)
    if data is None:
        say("   unreadable")
        return
    offset = 0
    while offset + 8 <= len(data):
        text = fstring_text(data, offset)
        if text is not None:
            say(f"   {BLOCK_START - BEFORE_BYTES + offset:#x} ({offset - BEFORE_BYTES:+d}): FString {text!r}")
            offset += 16
            continue
        word = u64(data, offset)
        if word:
            note = f"{word:#x}"
            if BLOCK_START - BEFORE_BYTES <= word < BLOCK_END:
                note += "  <- points into the block"
            say(f"   {BLOCK_START - BEFORE_BYTES + offset:#x} ({offset - BEFORE_BYTES:+d}): {note}")
        offset += 8


def pointers_into_block(label: str, address: int, size: int, depth: int, seen: set[int]) -> None:
    if address in seen or size <= 0:
        return
    seen.add(address)
    data = read(address, size)
    if data is None:
        return
    for offset in range(0, len(data) - 7, 8):
        word = u64(data, offset)
        if BLOCK_START - BEFORE_BYTES <= word < BLOCK_END:
            say(f"   {label} +{offset} ({address + offset:#x}) -> {word:#x} (block {word - BLOCK_START:+d})")
    if depth > 0:
        for offset in range(0, len(data) - 7, 8):
            word = u64(data, offset)
            if is_pointer(word) and not (BLOCK_START - BEFORE_BYTES <= word < BLOCK_END):
                pointers_into_block(f"{label}+{offset}->", word, 2048, depth - 1, seen)


def object_size(obj: UObject) -> int:
    try:
        return int(obj.Class._get_struct_size())
    except Exception:  # noqa: BLE001
        return 1024


def dump_owner_candidates() -> None:
    say("step 2: pointers into the block from the usual owners")
    pc = get_pc()
    roots: list[tuple[str, UObject]] = []
    if pc is not None:
        roots.append(("pc", pc))
        try:
            roots.append(("hud", pc.MyHUD))
        except Exception:  # noqa: BLE001
            pass
        try:
            roots.append(("player", pc.Player))
        except Exception:  # noqa: BLE001
            pass
        try:
            roots.append(("pawn", pc.Pawn))
        except Exception:  # noqa: BLE001
            pass
    for class_name in ("OakUIDataCollector_ItemCard", "OakUIDataCollector_ItemIcon", "OakUIDataCollector_Backpack", "OakUIDataCollector_EquippedAmmo", "OakUIDataCollector_LootFeed", "OakUIDataCollector_InteractPrompt", "GbxUISystem", "GbxUIView", "GbxUIViewManager", "OakGameInstance", "CohtmlBaseComponent", "CohtmlWidget"):
        try:
            for obj in unrealsdk.find_all(class_name, exact=False):
                if not str(obj.Name).startswith("Default__"):
                    roots.append((f"{class_name}:{obj.Name}", obj))
        except ValueError:
            continue
    seen: set[int] = set()
    for label, obj in roots:
        if obj is None:
            continue
        address = int(obj._get_address())
        pointers_into_block(label, address, object_size(obj), 1, seen)
    say(f"   {len(roots)} root object(s) checked")


def dump_neighbours() -> None:
    say("step 3: UObjects near the block")
    low, high = BLOCK_START - NEAR_BYTES, BLOCK_END + NEAR_BYTES
    found: list[tuple[int, str]] = []
    for obj in unrealsdk.find_all("Object", exact=False):
        try:
            address = int(obj._get_address())
        except Exception:  # noqa: BLE001
            continue
        if low <= address < high:
            found.append((address, f"{obj.Class.Name} {obj._path_name()}"))
    for address, label in sorted(found)[:60]:
        say(f"   {address:#x} ({address - BLOCK_START:+d}): {label}")
    say(f"   {len(found)} object(s) within 64 KB")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v27: the owner of the item model block")
try:
    dump_before()
    dump_owner_candidates()
    dump_neighbours()
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
say("done")
