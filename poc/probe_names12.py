"""
Phase 0 probe #27: does a UI data collector hold the finished name?

The UI is Coherent (HTML/JS). The item card is fed by `OakUIDataCollector_ItemCard`, whose
`OnWeaponChanged(NewWeapon, OldWeapon)` event hints that it also drives the weapon swap card.
Its data model is a C++ struct without reflected properties, so this probe reads the live
collector instances raw and chases their pointers two levels deep, looking for an FString
that holds FULL_NAME (or any string containing it). The same is done for the identity's
heap arrays. Everything is bounded: at most READ_BUDGET_BYTES read, TIME_BUDGET_S spent.

    pyexec probe_names12.py    right after swapping to the gun, with the inventory closed

Every raw read is checked against VirtualQuery first. No hooks, no calls into the game.
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

OUT = Path(__file__).parent / "probe_output.txt"
FULL_NAME = "Maintained Screeching T.K's Wave"
NEEDLE = FULL_NAME.encode("utf-16-le")
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
BLOCK_BYTES = 1024
DEEP_BLOCK_BYTES = 512
READ_BUDGET_BYTES = 48 * 1024 * 1024
TIME_BUDGET_S = 12.0
MAX_STRING_CHARS = 512
COLLECTOR_CLASSES = (
    "OakUIDataCollector_ItemCard",
    "OakUIDataCollector_ItemIcon",
    "OakUIDataCollector_Backpack",
    "OakUIDataCollector_LootFeed",
    "OakUIDataCollector_InteractPrompt",
    "OakUIDataCollector_EquippedAmmo",
    "OakUIScript_ItemInspect",
    "GbxUICoherentSubsystem",
    "GbxUIViewManager",
    "GbxUIView",
)


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
    logging.info(f"[dmgnames12] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


class Budget:
    def __init__(self) -> None:
        self.started = time.time()
        self.read_bytes = 0
        self.reads = 0

    def spent(self) -> bool:
        return self.read_bytes > READ_BUDGET_BYTES or time.time() - self.started > TIME_BUDGET_S

    def summary(self) -> str:
        return f"{self.reads} reads, {self.read_bytes / 1e6:.1f} MB, {time.time() - self.started:.1f} s"


budget = Budget()
region_cache: dict[int, tuple[int, int, bool]] = {}


def region_of(address: int) -> tuple[int, int, bool]:
    """(base, end, readable) of the OS region holding address; cached per page."""
    page = address & ~0xFFF
    cached = region_cache.get(page)
    if cached is not None:
        return cached
    info = MemoryBasicInformation()
    if kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
        result = (page, page + 0x1000, False)
    else:
        base = info.BaseAddress or 0
        ok = (
            info.State == MEM_COMMIT
            and not info.Protect & PAGE_GUARD
            and not info.Protect & PAGE_NOACCESS
            and any(info.Protect & flag for flag in READABLE_PROTECTIONS)
        )
        result = (base, base + info.RegionSize, ok)
    region_cache[page] = result
    return result


def read(address: int, size: int) -> bytes | None:
    """Bytes at address, clipped to the readable region; None when not readable at all."""
    if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
        return None
    base, end, ok = region_of(address)
    if not ok:
        return None
    size = min(size, end - address)
    if size <= 0:
        return None
    budget.reads += 1
    budget.read_bytes += size
    return ctypes.string_at(address, size)


def u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def i32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def is_pointer(word: int) -> bool:
    return POINTER_MIN <= word <= POINTER_MAX


def fstring_with_name(data: bytes, offset: int) -> str | None:
    """The text of an FString header {data*, num, max} at data[offset], when it holds the name."""
    if offset + 16 > len(data):
        return None
    pointer, num, capacity = u64(data, offset), i32(data, offset + 8), i32(data, offset + 12)
    if not is_pointer(pointer) or num < len(FULL_NAME) + 1 or num > MAX_STRING_CHARS or capacity < num:
        return None
    raw = read(pointer, num * 2)
    if raw is None or NEEDLE not in raw:
        return None
    try:
        return raw.decode("utf-16-le", errors="replace").split(chr(0), 1)[0]
    except Exception:  # noqa: BLE001
        return None


def scan_block(label: str, data: bytes, base: int, findings: list[str]) -> None:
    """Report FString headers in data that hold the name, and raw copies of it."""
    for offset in range(0, len(data) - 15, 8):
        text = fstring_with_name(data, offset)
        if text is not None:
            findings.append(f"{label} +{offset} ({base + offset:#x}): FString {text!r} (num {i32(data, offset + 8)})")
    position = data.find(NEEDLE)
    while position >= 0:
        findings.append(f"{label} +{position} ({base + position:#x}): raw UTF-16 copy")
        position = data.find(NEEDLE, position + 1)


def chase(label: str, address: int, size: int, findings: list[str], depth: int, seen: set[int]) -> None:
    """Scan the block at address and, for depth > 0, every block its pointers lead to."""
    if budget.spent() or address in seen:
        return
    seen.add(address)
    data = read(address, size)
    if data is None:
        return
    scan_block(label, data, address, findings)
    if depth <= 0:
        return
    next_size = BLOCK_BYTES if depth >= 2 else DEEP_BLOCK_BYTES
    for offset in range(0, len(data) - 7, 8):
        word = u64(data, offset)
        if is_pointer(word) and word != address:
            chase(f"{label}+{offset}->", word, next_size, findings, depth - 1, seen)
            if budget.spent():
                return


def live_instances(class_name: str) -> list[Any]:
    found = []
    try:
        for obj in unrealsdk.find_all(class_name, exact=True):
            if not str(obj.Name).startswith("Default__"):
                found.append(obj)
    except ValueError:
        pass
    return found


def probe_collectors() -> None:
    say("step 1: the UI collectors, chased two levels deep")
    for class_name in COLLECTOR_CLASSES:
        instances = live_instances(class_name)
        if not instances:
            say(f"   {class_name}: no live instance")
            continue
        for obj in instances:
            address = int(obj._get_address())
            size = int(obj.Class._get_struct_size())
            findings: list[str] = []
            chase(f"{class_name}", address, size, findings, 2, set())
            say(f"   {obj._path_name()} at {address:#x}, {size} bytes: {len(findings)} finding(s); {budget.summary()}")
            for line in findings[:40]:
                say(f"      {line}")
            if budget.spent():
                say("   budget spent")
                return


def probe_identity(weapon: Any) -> None:
    say("step 2: the identity's heap arrays, chased two levels deep")
    identity = int(weapon.Item.data.Identity._get_address())
    header = read(identity, 216)
    if header is None:
        say("   identity unreadable")
        return
    findings: list[str] = []
    seen: set[int] = set()
    for array_offset in (32, 48, 64, 88):
        data_pointer, num = u64(header, array_offset), i32(header, array_offset + 8)
        if not is_pointer(data_pointer) or num <= 0 or num > 64:
            continue
        chase(f"identity+{array_offset}", data_pointer, min(num * 40, 4096), findings, 2, seen)
    say(f"   {len(findings)} finding(s); {budget.summary()}")
    for line in findings[:40]:
        say(f"      {line}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v12: looking for {FULL_NAME!r} behind the UI collectors")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
try:
    probe_collectors()
    if weapon is not None and not budget.spent():
        probe_identity(weapon)
except Exception as ex:  # noqa: BLE001
    say(f"   failed: {type(ex).__name__}: {ex}")
say(f"done ({budget.summary()})")
