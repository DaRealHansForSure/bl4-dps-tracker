"""
Phase 0 probe #25: does the game keep the finished name anywhere?

The parts give the title ("T.K's Wave", from the barrel's naming aspect) and the type
definition gives the base name ("Jakobs Shotgun", MaxNumPrefixes 3); the prefixes come out
of a selection the naming strategy makes. Before reproducing that selection, check whether
the composed name the inventory shows exists in memory at all, and where: a cached string
next to the item would be the shortest road.

Scans every readable region of the process for FULL_NAME as UTF-16 and UTF-8, prints each
hit with its region and the bytes around it, and for the first hits looks for an FString
header (data pointer, length) and for pointers to that header, to see what owns the text.

    pyexec probe_names10.py    with the gun in hand, right after the inventory was open

The game stands still for the scan (budget SCAN_BUDGET_S). Memory is only read where the
OS reports it readable; no hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
from pathlib import Path

from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"
FULL_NAME = "Maintained Screeching T.K's Wave"
SCAN_BUDGET_S = 45.0
MAX_HITS = 12
MAX_REGION_BYTES = 1024 * 1024 * 1024
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)


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


def say(msg: str) -> None:
    logging.info(f"[dmgnames10] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


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
            and size <= MAX_REGION_BYTES
        )
        yield base, size, int(info.Type), usable
        address = base + size


def readable(address: int, size: int) -> bool:
    if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
        return False
    info = MemoryBasicInformation()
    if kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
        return False
    end = (info.BaseAddress or 0) + info.RegionSize
    return (
        info.State == MEM_COMMIT
        and not info.Protect & PAGE_GUARD
        and not info.Protect & PAGE_NOACCESS
        and any(info.Protect & flag for flag in READABLE_PROTECTIONS)
        and address + size <= end
    )


def scan(needles: dict[str, bytes], budget_s: float) -> dict[str, list[tuple[int, int, int, bytes]]]:
    """needle label -> [(hit address, region base, region type, context bytes)]."""
    hits: dict[str, list] = {label: [] for label in needles}
    started = time.time()
    scanned = 0
    for base, size, region_type, usable in regions():
        if time.time() - started > budget_s:
            say(f"   budget spent after {scanned / 1e6:.0f} MB")
            break
        if not usable:
            continue
        try:
            data = ctypes.string_at(base, size)
        except Exception:  # noqa: BLE001
            continue
        scanned += size
        for label, needle in needles.items():
            if len(hits[label]) >= MAX_HITS:
                continue
            position = data.find(needle)
            while position >= 0 and len(hits[label]) < MAX_HITS:
                context = data[max(position - 64, 0):position + len(needle) + 64]
                hits[label].append((base + position, base, region_type, context))
                position = data.find(needle, position + 1)
    say(f"   scanned {scanned / 1e6:.0f} MB in {time.time() - started:.1f} s")
    return hits


def pointers_to(value: int, budget_s: float, limit: int = 8) -> list[int]:
    """Addresses holding this 8-byte value, anywhere readable, within the budget."""
    needle = value.to_bytes(8, "little")
    found: list[int] = []
    started = time.time()
    for base, size, _type, usable in regions():
        if time.time() - started > budget_s or len(found) >= limit:
            break
        if not usable:
            continue
        try:
            data = ctypes.string_at(base, size)
        except Exception:  # noqa: BLE001
            continue
        position = data.find(needle)
        while position >= 0 and len(found) < limit:
            if (base + position) % 8 == 0:
                found.append(base + position)
            position = data.find(needle, position + 1)
    return found


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v10: looking for {FULL_NAME!r}")
needles = {"utf-16": FULL_NAME.encode("utf-16-le"), "utf-8": FULL_NAME.encode("utf-8")}
results = scan(needles, SCAN_BUDGET_S)
for label, found in results.items():
    say(f"   {label}: {len(found)} hit(s)")
    for address, base, region_type, context in found:
        say(f"      {address:#x} in region {base:#x} (type {region_type:#x})")
        say(f"         context: {context.hex()}")
        if label == "utf-16":
            # An FString header {data*, num, max} pointing at this buffer, anywhere?
            headers = pointers_to(address, 8.0, 4)
            say(f"         FString headers pointing here: {[f'{h:#x}' for h in headers]}")
            for header in headers:
                if readable(header, 16):
                    raw = ctypes.string_at(header, 16)
                    say(f"            header {header:#x}: num {int.from_bytes(raw[8:12], 'little')} max {int.from_bytes(raw[12:16], 'little')}; owners {[f'{o:#x}' for o in pointers_to(header, 8.0, 4)]}")
say("done")
