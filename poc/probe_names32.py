"""
Phase 0 probe #45: the ownership chain of the item card records, three scans in one go.

This session's records array data starts at RECORDS (the shotgun's name header was found at
RECORDS + 3 * 280 + 32). Pass 1 finds every word equal to RECORDS: the TArray header inside
the 64-byte node. Pass 2 walks back over node-like 64-byte entries to the start of the node
array and finds every word equal to that start (and to the node itself): the node array's
header inside its owner. Pass 3 finds every word equal to the owner candidates found in
pass 2 (their 64-byte aligned starts), so the chain can be followed up to something a
UObject holds. Every owner is printed with 128 bytes of context and, when it lies inside a
UObject, that object's class and path.

Runs in a background thread (ReadProcessMemory, 8 MB slices), about 5 minutes per pass.
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
RECORDS = 0x1791FC020
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
CHUNK_BYTES = 8 * 1024 * 1024
PASS_BUDGET_S = 330.0
MAX_OWNERS = 6
NODE_BYTES = 64


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
        logging.info(f"[dmgnames32] {msg}")
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


def find_owners(values: list[int], budget_s: float) -> dict[int, list[int]]:
    """value -> addresses of 8-aligned words holding it, over the whole process."""
    started = time.time()
    needles = {value: value.to_bytes(8, "little") for value in values}
    owners: dict[int, list[int]] = {value: [] for value in values}
    scanned = 0
    for base, size, usable in regions():
        if time.time() - started > budget_s:
            say(f"   pass budget spent after {scanned / 1e9:.1f} GB")
            break
        if not usable:
            continue
        for chunk_start in range(base, base + size, CHUNK_BYTES):
            data = read(chunk_start, min(CHUNK_BYTES, base + size - chunk_start))
            if data is None:
                continue
            scanned += len(data)
            for value, needle in needles.items():
                if len(owners[value]) >= MAX_OWNERS:
                    continue
                position = data.find(needle)
                while position >= 0 and len(owners[value]) < MAX_OWNERS:
                    if position % 8 == 0:
                        owners[value].append(chunk_start + position)
                    position = data.find(needle, position + 1)
            time.sleep(0.002)
    say(f"   scanned {scanned / 1e9:.1f} GB in {time.time() - started:.0f} s")
    return owners


def looks_like_node(address: int) -> bool:
    data = read(address, NODE_BYTES)
    if data is None or len(data) < NODE_BYTES:
        return False
    pointer, num, capacity = u64(data, 0), i32(data, 8), i32(data, 12)
    if pointer == 0 and num == 0 and capacity == 0:
        return True
    return is_pointer(pointer) and num == 1 and capacity == 1


def describe(label: str, address: int, objects: dict[int, tuple[int, str]]) -> None:
    data = read(address - 64, 192)
    if data is None:
        say(f"   {label} {address:#x}: unreadable")
        return
    say(f"   {label} {address:#x}: header {{num {i32(data, 72)}, max {i32(data, 76)}}}")
    say(f"      before {hex_words(data[:64])}")
    say(f"      after  {hex_words(data[64:])}")
    for start, (size, path) in objects.items():
        if start <= address < start + size:
            say(f"      inside UObject {path} at +{address - start} (size {size})")


def scan_chain(objects: dict[int, tuple[int, str]]) -> None:
    say("pass 1: words equal to the records array data")
    node_headers = find_owners([RECORDS], PASS_BUDGET_S)[RECORDS]
    say(f"   {len(node_headers)} candidate(s): {[hex(h) for h in node_headers]}")
    for header in node_headers:
        describe("node header", header, objects)
    if not node_headers:
        say("chain scan finished: nothing points at the records")
        return
    # The node is 16 bytes before the header when it starts with the type-ops array.
    nodes = []
    for header in node_headers:
        node = header - 16 if looks_like_node(header - 16) else header
        start = node
        while looks_like_node(start - NODE_BYTES):
            start -= NODE_BYTES
        count = (node - start) // NODE_BYTES
        say(f"   node {node:#x}, node run starts at {start:#x} (index {count})")
        nodes.append((node, start))
    values = []
    for node, start in nodes:
        values += [node, start]
    say("pass 2: words equal to the node and to the start of its run")
    owners = find_owners(sorted(set(values)), PASS_BUDGET_S)
    next_values = []
    for value, holders in owners.items():
        say(f"   {value:#x}: {len(holders)} holder(s)")
        for holder in holders:
            describe("holder", holder, objects)
            next_values.append(holder & ~0x3F)
    if not next_values:
        say("chain scan finished: nothing points at the node")
        return
    say("pass 3: words equal to the holders' 64-byte aligned starts")
    owners = find_owners(sorted(set(next_values)), PASS_BUDGET_S)
    for value, holders in owners.items():
        say(f"   {value:#x}: {len(holders)} holder(s)")
        for holder in holders:
            describe("holder", holder, objects)
    say("chain scan finished")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v32: the ownership chain of the records at {RECORDS:#x}")
try:
    objects: dict[int, tuple[int, str]] = {}
    for obj in unrealsdk.find_all("Object", exact=False):
        try:
            objects[int(obj._get_address())] = (int(obj.Class._get_struct_size()), f"{obj.Class.Name} {obj._path_name()}")
        except Exception:  # noqa: BLE001
            continue
    say(f"   {len(objects)} UObject(s) indexed")
    threading.Thread(target=scan_chain, args=(objects,), name="dmgnames32", daemon=True).start()
    say("   scanning in the background; the game keeps running")
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
