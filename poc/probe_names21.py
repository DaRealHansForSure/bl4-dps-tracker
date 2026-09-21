"""
Phase 0 probe #36: find the inventory globals anywhere in the process, without a freeze.

The OakGlobalsInventory instance is not in the definition arena (probes 13, 14, 18, 20),
yet its InventoryNamingAttributes map is the last unknown of the naming rules. This probe
scans every committed region of the process in a background thread, reading through
ReadProcessMemory (which fails cleanly on pages that vanish) in 8 MB chunks, so the game
thread only competes for the interpreter lock in short slices. Two signatures:

- the FName "Weapon" (BaseWeaponTypeName, +120) followed 504 bytes later by a TMap header
  with 8 elements (InventoryNamingAttributes, +624) and 1888 bytes later by an array of
  5..32 entries (RarityDefOrder, +2008);
- a run of at least 5 consecutive def pointers whose type word is InventoryRarityDef.

Candidates are appended to probe_output.txt and written to probe_names21_result.json for
probe_names22 to read typed on the game thread.

    pyexec probe_names21.py    then wait for "scan finished" in the log (about a minute)

Only scratch memory the SDK owns is written. No hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import threading
import time
from pathlib import Path

import unrealsdk
from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"
RESULT = Path(__file__).parent / "probe_names21_result.json"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
CHUNK_BYTES = 8 * 1024 * 1024
TIME_BUDGET_S = 240.0
DEF_PTR_SIZE = 24
GLOBALS_WEAPON_NAME_OFFSET = 120
GLOBALS_MAP_OFFSET = 624
GLOBALS_RARITY_ORDER_OFFSET = 2008
MIN_RARITY_RUN = 5
MAX_CANDIDATES = 12
SCRATCH_NAME_STRUCT = "InventoryPartCriterionData"
RARITY_STRUCT = "/Script/GbxGame.InventoryRarityDef"


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
        logging.info(f"[dmgnames21] {msg}")
    except Exception:  # noqa: BLE001
        pass
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def read_memory(address: int, size: int) -> bytes | None:
    """Bytes at address through ReadProcessMemory: no access violation on a vanished page."""
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


def looks_like_globals(instance: int) -> bool:
    """The map at +624 has 8 elements and the rarity order at +2008 has 5..32."""
    map_header = read_memory(instance + GLOBALS_MAP_OFFSET, 16)
    order_header = read_memory(instance + GLOBALS_RARITY_ORDER_OFFSET, 16)
    if map_header is None or order_header is None:
        return False
    map_ok = is_pointer(u64(map_header, 0)) and i32(map_header, 8) == 8
    order_ok = is_pointer(u64(order_header, 0)) and MIN_RARITY_RUN <= i32(order_header, 8) <= 32
    return map_ok and order_ok


def scan(weapon_name: bytes, rarity_struct_address: int) -> None:
    started = time.time()
    scanned = 0
    globals_candidates: list[int] = []
    rarity_runs: list[tuple[int, int]] = []
    struct_needle = rarity_struct_address.to_bytes(8, "little")
    for base, size, usable in regions():
        if time.time() - started > TIME_BUDGET_S:
            say("   time budget spent")
            break
        if not usable:
            continue
        for chunk_start in range(base, base + size, CHUNK_BYTES):
            chunk_size = min(CHUNK_BYTES, base + size - chunk_start)
            overlap = min(DEF_PTR_SIZE * MIN_RARITY_RUN, base + size - chunk_start - chunk_size)
            data = read_memory(chunk_start, chunk_size + overlap)
            if data is None:
                continue
            scanned += chunk_size
            position = data.find(weapon_name)
            while position >= 0 and len(globals_candidates) < MAX_CANDIDATES:
                if position % 8 == 0:
                    instance = chunk_start + position - GLOBALS_WEAPON_NAME_OFFSET
                    if looks_like_globals(instance):
                        globals_candidates.append(instance)
                        say(f"   globals candidate at {instance:#x}")
                position = data.find(weapon_name, position + 1)
            position = data.find(struct_needle)
            while position >= 0 and len(rarity_runs) < MAX_CANDIDATES:
                if position % 8 == 0 and position >= 8 and (position < chunk_size):
                    run = 1
                    while position + run * DEF_PTR_SIZE + 8 <= len(data) and data[position + run * DEF_PTR_SIZE:position + run * DEF_PTR_SIZE + 8] == struct_needle:
                        run += 1
                    previous_is_same = position >= DEF_PTR_SIZE and data[position - DEF_PTR_SIZE:position - DEF_PTR_SIZE + 8] == struct_needle
                    if run >= MIN_RARITY_RUN and not previous_is_same:
                        rarity_runs.append((chunk_start + position - 8, run))
                        say(f"   run of {run} rarity def pointers at {chunk_start + position - 8:#x}")
                position = data.find(struct_needle, position + 1)
            time.sleep(0.002)
    say(f"   scanned {scanned / 1e9:.1f} GB in {time.time() - started:.0f} s; {len(globals_candidates)} globals candidate(s), {len(rarity_runs)} rarity run(s)")
    # Second pass only when a rarity run exists but no direct candidate: who points at it?
    headers: list[int] = []
    if rarity_runs and not globals_candidates and time.time() - started < TIME_BUDGET_S:
        needles = [start.to_bytes(8, "little") for start, _run in rarity_runs]
        for base, size, usable in regions():
            if time.time() - started > TIME_BUDGET_S or len(headers) >= MAX_CANDIDATES:
                break
            if not usable:
                continue
            for chunk_start in range(base, base + size, CHUNK_BYTES):
                chunk_size = min(CHUNK_BYTES, base + size - chunk_start)
                data = read_memory(chunk_start, chunk_size + min(16, base + size - chunk_start - chunk_size))
                if data is None:
                    continue
                for needle in needles:
                    position = data.find(needle)
                    while position >= 0 and len(headers) < MAX_CANDIDATES:
                        if position % 8 == 0 and position + 16 <= len(data):
                            num = i32(data, position + 8)
                            if MIN_RARITY_RUN <= num <= 32:
                                headers.append(chunk_start + position)
                                say(f"   array header at {chunk_start + position:#x} (num {num}) -> globals candidate {chunk_start + position - GLOBALS_RARITY_ORDER_OFFSET:#x}")
                        position = data.find(needle, position + 1)
                time.sleep(0.002)
    result = {
        "globals": [f"{address:#x}" for address in globals_candidates],
        "rarity_runs": [f"{start:#x}" for start, _run in rarity_runs],
        "headers": [f"{header:#x}" for header in headers],
    }
    RESULT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    say(f"scan finished in {time.time() - started:.0f} s -> {RESULT.name}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v21: background scan for the inventory globals")
try:
    scratch = unrealsdk.make_struct(SCRATCH_NAME_STRUCT)
    name_prop = next(p for p in scratch._type._properties() if str(p.Class.Name) == "NameProperty")
    scratch.Name = "Weapon"
    weapon_name = ctypes.string_at(int(scratch._get_address()) + int(name_prop.Offset_Internal), 8)
    rarity_struct = unrealsdk.find_object("ScriptStruct", RARITY_STRUCT)
    rarity_struct_address = int(rarity_struct._get_address())
    say(f"   FName 'Weapon' raw {weapon_name.hex()}, InventoryRarityDef struct {rarity_struct_address:#x}")
    worker = threading.Thread(target=scan, args=(weapon_name, rarity_struct_address), name="dmgnames21", daemon=True)
    worker.start()
    say("   scanning in the background; the game keeps running")
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
