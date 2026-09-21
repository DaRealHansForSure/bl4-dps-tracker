"""
Phase 0 probe #43: who points at the item model arrays (their first elements)?

A memory scanner found "Maintained Screeching" (the licensed + attribute prefix, without
the title) at eight addresses that stay stable. If those are per-item records the mod can
reach (an inventory model with the item handle next to the strings), the name can be read
once per item and cached under the seed id. This probe scans the process in a background
thread (ReadProcessMemory, 8 MB slices) for pointers to each address, then dumps 160 bytes
before and after every owner with FString headers, names and handles decoded.

    pyexec probe_names25.py    then wait for "owner scan finished" in the log

Edit ADDRESSES when the game was restarted (the scanner's addresses change per session).
Raw reads go through ReadProcessMemory. No hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import threading
import time
from pathlib import Path

import unrealsdk
from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"
ADDRESSES = (0x2D0BA9A88, 0x2D0BAA028, 0x2D0BAA4D8, 0x2D0BA9A10, 0x2D0BA9A80, 0x2D0BAA030, 0x2D0BA9BB8, 0x2D0BAA038)
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
CHUNK_BYTES = 8 * 1024 * 1024
TIME_BUDGET_S = 300.0
MAX_OWNERS_PER_ADDRESS = 8
CONTEXT_BYTES = 160
MAX_TEXT_CHARS = 120
SCRATCH_NAME_STRUCT = "InventoryPartCriterionData"


class MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


kernel32 = ctypes.windll.kernel32
kernel32.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.POINTER(MemoryBasicInformation), ctypes.c_size_t)
kernel32.VirtualQuery.restype = ctypes.c_size_t
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
kernel32.ReadProcessMemory.restype = wintypes.BOOL
PROCESS = kernel32.GetCurrentProcess()


def say(msg: str) -> None:
    try:
        logging.info(f"[dmgnames28] {msg}")
    except Exception:  # noqa: BLE001
        pass
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


def regions():
    address = 0
    while address < POINTER_MAX:
        info = MemoryBasicInformation()
        if kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
            break
        base = info.BaseAddress or 0
        size = info.RegionSize
        usable = (
            info.State == MEM_COMMIT
            and not info.Protect & PAGE_GUARD
            and not info.Protect & PAGE_NOACCESS
            and any(info.Protect & flag for flag in READABLE_PROTECTIONS)
        )
        yield base, size, usable
        address = base + size


def u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def i32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def is_pointer(word: int) -> bool:
    return POINTER_MIN <= word <= POINTER_MAX


def hex_words(data: bytes) -> str:
    return " ".join(data[i:i + 8].hex() for i in range(0, len(data) - 7, 8))


def utf16_at(address: int, limit: int = MAX_TEXT_CHARS) -> str | None:
    raw = read(address, limit * 2)
    if raw is None or len(raw) < 4:
        return None
    end = raw.find(b"\x00\x00")
    if end % 2:
        end += 1
    chunk = raw[:end] if end >= 0 else raw
    try:
        text = chunk.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if len(text) < 2 or not all(32 <= ord(ch) < 0x3000 for ch in text):
        return None
    return text


def fstring_text(data: bytes, offset: int) -> str | None:
    if offset + 16 > len(data):
        return None
    pointer, num, capacity = u64(data, offset), i32(data, offset + 8), i32(data, offset + 12)
    if not is_pointer(pointer) or num < 2 or num > 512 or capacity < num or capacity > 4096:
        return None
    return utf16_at(pointer, min(num, MAX_TEXT_CHARS))


def describe_context(owner: int, name_words: dict[bytes, str]) -> None:
    start = owner - CONTEXT_BYTES
    data = read(start, CONTEXT_BYTES * 2 + 16)
    if data is None:
        say("      context unreadable")
        return
    say(f"      before: {hex_words(data[:CONTEXT_BYTES])}")
    say(f"      after:  {hex_words(data[CONTEXT_BYTES:])}")
    notes = []
    for offset in range(0, len(data) - 15, 8):
        text = fstring_text(data, offset)
        if text is not None:
            notes.append(f"{offset - CONTEXT_BYTES:+d}: FString {text!r}")
    for offset in range(0, len(data) - 7, 8):
        word = data[offset:offset + 8]
        name = name_words.get(word)
        if name is not None:
            notes.append(f"{offset - CONTEXT_BYTES:+d}: name {name!r}")
    for line in notes[:40]:
        say(f"      {line}")


def scan(name_words: dict[bytes, str]) -> None:
    started = time.time()
    needles = {address: address.to_bytes(8, "little") for address in ADDRESSES}
    owners: dict[int, list[int]] = {address: [] for address in ADDRESSES}
    scanned = 0
    for base, size, usable in regions():
        if time.time() - started > TIME_BUDGET_S:
            say("   time budget spent")
            break
        if not usable:
            continue
        for chunk_start in range(base, base + size, CHUNK_BYTES):
            data = read(chunk_start, min(CHUNK_BYTES, base + size - chunk_start))
            if data is None:
                continue
            scanned += len(data)
            for address, needle in needles.items():
                if len(owners[address]) >= MAX_OWNERS_PER_ADDRESS:
                    continue
                position = data.find(needle)
                while position >= 0 and len(owners[address]) < MAX_OWNERS_PER_ADDRESS:
                    if position % 8 == 0:
                        owners[address].append(chunk_start + position)
                    position = data.find(needle, position + 1)
            time.sleep(0.002)
    say(f"   scanned {scanned / 1e9:.1f} GB in {time.time() - started:.0f} s")
    for address in ADDRESSES:
        text = utf16_at(address)
        say(f"   {address:#x} {text!r}: {len(owners[address])} owner(s)")
        for owner in owners[address]:
            header = read(owner, 16)
            num, capacity = (i32(header, 8), i32(header, 12)) if header and len(header) >= 16 else (None, None)
            say(f"      owner word at {owner:#x} (as FString header: num {num} max {capacity})")
            describe_context(owner, name_words)
    say("owner scan finished")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v28: owners of the item model arrays (array starts)")
try:
    name_words: dict[bytes, str] = {}
    scratch = unrealsdk.make_struct(SCRATCH_NAME_STRUCT)
    name_prop = next(p for p in scratch._type._properties() if str(p.Class.Name) == "NameProperty")
    name_address = int(scratch._get_address()) + int(name_prop.Offset_Internal)
    for text in ("Weapon", "Shield", "Backpack", "Bank", "Equipment", "Name", "Title", "Prefix", "Rarity", "ItemCard", "Inventory", "Item", "Level", "Manufacturer", "damage", "firerate"):
        scratch.Name = text
        name_words[ctypes.string_at(name_address, 8)] = text
    threading.Thread(target=scan, args=(name_words,), name="dmgnames25", daemon=True).start()
    say("   scanning for owners in the background; the game keeps running")
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
