"""
Phase 0 probe #47: locate the item cards through an equipped gun's seed bytes.

In session 1 the UI block was one struct: 5 slot records of 288 bytes, each with a copy of
the slot's item identity (the twelve seed bytes at record + 248), then 8 bytes, then the
4 item card records of 280 bytes. The seed of an equipped gun is known from its actor, so
one search for those twelve bytes finds the slot record, and the card records follow at a
fixed distance. This probe searches the committed private read-write memory for the seed
of the gun in hand on a background thread (whole regions read at once, a short sleep every
few megabytes), lists every hit with its surroundings, and checks the card records that
the fixed layout predicts for each hit.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import threading
import time
from pathlib import Path

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging

import dmgtracker.itemnames as itemnames

OUT = Path(__file__).parent / "probe_output.txt"
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_READWRITE = 0x04
READ_BYTES = 16 * 1024 * 1024
SLEEP_EVERY_BYTES = 4 * 1024 * 1024
SLEEP_S = 0.003
SEED_OFFSET_IN_IDENTITY = 200
IDENTITY_OFFSET_IN_SLOT_RECORD = 48
SLOT_RECORD_BYTES = 288
SLOT_RECORDS = 5
CARDS_GAP_BYTES = 8
CARD_RECORD_BYTES = 280
MAX_HITS = 16


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


kernel32 = ctypes.WinDLL("kernel32")
kernel32.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
kernel32.VirtualQuery.restype = ctypes.c_size_t
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
kernel32.ReadProcessMemory.restype = wintypes.BOOL
PROCESS = kernel32.GetCurrentProcess()


def say(msg: str) -> None:
    try:
        logging.info(f"[dmgnames34] {msg}")
    except Exception:  # noqa: BLE001
        pass
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
        if info.State == MEM_COMMIT and info.Type == MEM_PRIVATE and info.Protect == PAGE_READWRITE:
            yield base, size
        address = base + size


def read(address: int, size: int, buffer: ctypes.Array) -> bytes | None:
    got = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(PROCESS, ctypes.c_void_p(address), buffer, size, ctypes.byref(got))
    if (not ok and got.value == 0) or got.value == 0:
        return None
    return buffer.raw[:got.value]


def find_all(needle: bytes, limit: int, budget_s: float) -> tuple[list[int], float, int, int]:
    started = time.perf_counter()
    buffer = ctypes.create_string_buffer(READ_BYTES)
    hits: list[int] = []
    scanned = 0
    since_sleep = 0
    region_count = 0
    for base, size in regions():
        region_count += 1
        for chunk_start in range(base, base + size, READ_BYTES):
            if time.perf_counter() - started > budget_s or len(hits) >= limit:
                return hits, time.perf_counter() - started, scanned, region_count
            chunk = read(chunk_start, min(READ_BYTES, base + size - chunk_start), buffer)
            if chunk is None:
                continue
            scanned += len(chunk)
            since_sleep += len(chunk)
            position = chunk.find(needle)
            while position >= 0 and len(hits) < limit:
                hits.append(chunk_start + position)
                position = chunk.find(needle, position + 1)
            if since_sleep >= SLEEP_EVERY_BYTES:
                since_sleep = 0
                time.sleep(SLEEP_S)
    return hits, time.perf_counter() - started, scanned, region_count


def run(seed: bytes, live_identity: int, label: str) -> None:
    reader = itemnames.ItemCardReader()
    hits, seconds, scanned, region_count = find_all(seed, MAX_HITS, 300.0)
    say(f"   seed of {label}: {len(hits)} hit(s) in {seconds:.1f} s over {scanned / 1e9:.2f} GB, {region_count} region(s): {[hex(h) for h in hits]}")
    for hit in hits:
        identity = hit - SEED_OFFSET_IN_IDENTITY
        note = "the live identity of the actor" if identity == live_identity else ""
        slot_record = identity - IDENTITY_OFFSET_IN_SLOT_RECORD
        found = None
        for index in range(SLOT_RECORDS):
            cards = slot_record - index * SLOT_RECORD_BYTES + SLOT_RECORDS * SLOT_RECORD_BYTES + CARDS_GAP_BYTES
            names = []
            for position in range(itemnames.MAX_RECORDS):
                card = reader._card_at(cards + position * CARD_RECORD_BYTES)
                if card is None:
                    break
                names.append(card.name)
            if names:
                found = (index, cards, names)
                break
        say(f"   hit {hit:#x} {note}: identity {identity:#x}, slot record {slot_record:#x}; cards {found}")
    say("done")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v34: item cards through the seed of the gun in hand")
try:
    pawn = get_pc().Pawn
    weapon = pawn.ActiveWeapons.Slots[0].Weapon
    identity = int(weapon.Item.data.Identity._get_address())
    buffer = ctypes.create_string_buffer(12)
    seed = read(identity + SEED_OFFSET_IN_IDENTITY, 12, buffer)
    say(f"   gun in hand {weapon.BodyData.Name}, identity {identity:#x}, seed {seed.hex()}")
    threading.Thread(target=run, args=(seed, identity, str(weapon.BodyData.Name)), name="dmgnames34", daemon=True).start()
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
