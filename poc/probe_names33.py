"""
Phase 0 probe #46: locate the item card records without an anchor, gently.

Session 3 put the records' node more than 64 MB away from the game instance's UI object,
so adjacency is not a locator. Every item card record holds an FString whose buffer starts
with TextureRenderTarget2D'/Engine/Transient.SceneCaptureComponent2D_ (the thumbnail
path), and those small string buffers live in the 0x13.. allocator family in every session
seen so far. This probe, on a background thread with small chunks and sleeps between them:

- pass A: finds the UTF-16 thumbnail prefix in the committed regions of that family (and,
  if nothing is found there, everywhere);
- pass B: finds the 8-byte pointers to those buffers anywhere, takes each as the thumbnail
  field of a record, validates the record and walks the run of records around it;
- reports the array, its distance to the anchor, and the time each pass took.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import threading
import time
from pathlib import Path

import unrealsdk
from unrealsdk import logging

import dmgtracker.itemnames as itemnames

OUT = Path(__file__).parent / "probe_output.txt"
THUMBNAIL_PREFIX = "TextureRenderTarget2D'/Engine/Transient.SceneCaptureComponent2D_".encode("utf-16-le")
STRING_FAMILY = (0x1300000000, 0x1400000000)
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_READWRITE = 0x04
CHUNK_BYTES = 1024 * 1024
SLEEP_S = 0.002
THUMBNAIL_FIELD_OFFSET = 56
RECORD_BYTES = 280
MAX_BUFFERS = 12
MAX_RECORDS = 8


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


def say(msg: str) -> None:
    try:
        logging.info(f"[dmgnames33] {msg}")
    except Exception:  # noqa: BLE001
        pass
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def regions(low: int = 0, high: int = POINTER_MAX):
    """Committed, private, read-write regions between low and high."""
    address = low
    while address < high:
        info = MemoryBasicInformation()
        if kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
            break
        base = info.BaseAddress or 0
        size = info.RegionSize
        if info.State == MEM_COMMIT and info.Type == MEM_PRIVATE and info.Protect == PAGE_READWRITE:
            yield base, size
        address = base + size


def u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def scan_regions(reader, low: int, high: int, needles: list[bytes], limit: int, budget_s: float) -> tuple[list[tuple[bytes, int]], float, int]:
    started = time.perf_counter()
    hits: list[tuple[bytes, int]] = []
    scanned = 0
    for base, size in regions(low, high):
        for chunk_start in range(base, base + size, CHUNK_BYTES):
            if time.perf_counter() - started > budget_s or len(hits) >= limit:
                return hits, time.perf_counter() - started, scanned
            chunk = reader._read(chunk_start, min(CHUNK_BYTES, base + size - chunk_start))
            if chunk is None:
                continue
            scanned += len(chunk)
            for needle in needles:
                position = chunk.find(needle)
                while position >= 0 and len(hits) < limit:
                    hits.append((needle, chunk_start + position))
                    position = chunk.find(needle, position + 1)
            time.sleep(SLEEP_S)
    return hits, time.perf_counter() - started, scanned


def run() -> None:
    reader = itemnames.ItemCardReader()
    gi = next(o for o in unrealsdk.find_all("OakGameInstance", exact=False) if not str(o.Name).startswith("Default__"))
    anchor = reader._anchor(int(gi._get_address())) or 0
    say(f"   anchor {anchor:#x}")
    say("pass A: thumbnail prefix in the string family")
    hits, seconds, scanned = scan_regions(reader, STRING_FAMILY[0], STRING_FAMILY[1], [THUMBNAIL_PREFIX], MAX_BUFFERS, 240.0)
    say(f"   {len(hits)} buffer(s) in {seconds:.1f} s over {scanned / 1e6:.0f} MB: {[hex(a) for _n, a in hits]}")
    if not hits:
        say("pass A2: thumbnail prefix everywhere")
        hits, seconds, scanned = scan_regions(reader, 0, POINTER_MAX, [THUMBNAIL_PREFIX], MAX_BUFFERS, 300.0)
        say(f"   {len(hits)} buffer(s) in {seconds:.1f} s over {scanned / 1e6:.0f} MB: {[hex(a) for _n, a in hits]}")
    if not hits:
        say("done: no thumbnail strings")
        return
    say("pass B: pointers to those buffers, everywhere")
    needles = [address.to_bytes(8, "little") for _n, address in hits]
    holders, seconds, scanned = scan_regions(reader, 0, POINTER_MAX, needles, 32, 300.0)
    say(f"   {len(holders)} holder(s) in {seconds:.1f} s over {scanned / 1e6:.0f} MB")
    arrays: dict[int, int] = {}
    for needle, holder in holders:
        record = holder - THUMBNAIL_FIELD_OFFSET
        card = reader._card_at(record)
        say(f"   holder {holder:#x} -> record {record:#x}: {card.name if card else 'not a card'}")
        if card is None:
            continue
        start = record
        while reader._card_at(start - RECORD_BYTES) is not None:
            start -= RECORD_BYTES
        count = 0
        while count < MAX_RECORDS and reader._card_at(start + count * RECORD_BYTES) is not None:
            count += 1
        arrays[start] = count
    for start, count in arrays.items():
        say(f"   array {start:#x} x{count} (anchor {start - anchor:+#x}): {[reader._card_at(start + i * RECORD_BYTES).name for i in range(count)]}")
    say("done")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v33: locating the item cards by their thumbnail strings")
threading.Thread(target=run, name="dmgnames33", daemon=True).start()
