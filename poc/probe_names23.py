"""
Phase 0 probe #38: the record that owns the rarity run at 0x151d6a80.

probe_names21 found a run of five InventoryRarityDef pointers at a low heap address,
outside the definition arena, but ran out of time before looking for the array header.
This probe reads the run itself (names of the rarity defs), then searches for an array
header {run start, num, max} in a window of 256 MB on each side of the run (a few seconds
on the game thread), and dumps every owner it finds as an OakGlobalsInventory if its map
at +624 and its rarity order at +2008 look right. If the window holds nothing, a background
scan of the whole low half of the address space follows and writes its candidates to
probe_names21_result.json for probe_names22.

    pyexec probe_names23.py

Raw reads go through ReadProcessMemory. Only scratch memory the SDK owns is written.
No hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import threading
import time
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import logging
from unrealsdk.unreal import FGbxDefPtr, UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
RESULT = Path(__file__).parent / "probe_names21_result.json"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
CHUNK_BYTES = 8 * 1024 * 1024
WINDOW_BYTES = 256 * 1024 * 1024
LOW_HALF_LIMIT = 0x800000000
TIME_BUDGET_S = 240.0
DEF_PTR_SIZE = 24
RUN_START = 0x151D6A80
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
RARITY_STRUCT = "/Script/GbxGame.InventoryRarityDef"
GLOBALS_STRUCT = "/Script/OakGame.OakGlobalsInventory"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
SCRATCH_NAME_STRUCT = "InventoryPartCriterionData"
GLOBALS_SIZE = 2064
GLOBALS_MAP_OFFSET = 624
GLOBALS_RARITY_ORDER_OFFSET = 2008
MAX_VALUE_CHARS = 2500
MAX_ITEMS = 64
MAX_TEXT_CHARS = 200


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
        logging.info(f"[dmgnames23] {msg}")
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


def readable(address: int, size: int) -> bool:
    data = read(address, size)
    return data is not None and len(data) == size


def regions(limit: int = POINTER_MAX):
    address = 0
    while address < limit:
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


def find_struct(path: str) -> Any:
    try:
        return unrealsdk.find_object("ScriptStruct", path)
    except ValueError:
        wanted = path.rsplit(".", 1)[-1]
        for struct in unrealsdk.find_all("ScriptStruct", exact=True):
            if str(struct.Name) == wanted:
                return struct
        raise


def fstring_at(address: int) -> str | None:
    header = read(address, 16)
    if header is None or len(header) < 16:
        return None
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    if num <= 1 or num > MAX_TEXT_CHARS or capacity < num or capacity > 4096:
        return None
    raw = read(data, num * 2)
    if raw is None or len(raw) < num * 2 or raw[-2:] != b"\x00\x00":
        return None
    try:
        text = raw[:-2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not text or not all(32 <= ord(ch) < 0x3000 for ch in text):
        return None
    return text


def def_label(value: Any) -> str:
    address = 0
    try:
        address = int(value._get_address())
    except Exception:  # noqa: BLE001
        pass
    name = kind_name = None
    try:
        name = value._name
        kind = value._type
        kind_name = str(kind.Name) if kind is not None else None
    except Exception:  # noqa: BLE001
        pass
    return f"<def {name} {kind_name} {address:#x}>"


def described(value: Any, depth: int = 0, max_depth: int = 4) -> str:
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, FGbxDefPtr):
            return def_label(value)
        if isinstance(value, WrappedStruct):
            if depth >= max_depth:
                return f"<struct {value._type.Name}>"
            parts = []
            for prop in value._type._properties():
                name = str(prop.Name)
                try:
                    parts.append(f"{name}={described(getattr(value, name), depth + 1, max_depth)}")
                except Exception as ex:  # noqa: BLE001
                    parts.append(f"{name}=<err {type(ex).__name__}>")
            return "{" + ", ".join(parts) + "}"
        if isinstance(value, WrappedArray):
            items = [described(item, depth + 1, max_depth) for item in list(value)[:MAX_ITEMS]]
            more = "" if len(value) <= MAX_ITEMS else f", ... {len(value)} items"
            return "[" + ", ".join(items) + more + "]"
        text = repr(value)
        return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"


class DefView:
    def __init__(self) -> None:
        self.scratch = unrealsdk.make_struct(SCRATCH_STRUCT)
        field = next(p for p in find_struct(TYPE_STRUCT)._properties() if str(p.Name) == SCRATCH_FIELD)
        self.field_address = int(self.scratch._get_address()) + int(field.Offset_Internal)
        self.original = ctypes.string_at(self.field_address, DEF_PTR_SIZE)

    def view(self, struct: Any, data_address: int) -> Any:
        triple = bytes(8) + int(struct._get_address()).to_bytes(8, "little") + data_address.to_bytes(8, "little")
        ctypes.memmove(self.field_address, triple, DEF_PTR_SIZE)
        return getattr(self.scratch, SCRATCH_FIELD)

    def restore(self) -> None:
        ctypes.memmove(self.field_address, self.original, DEF_PTR_SIZE)


class NameResolver:
    def __init__(self) -> None:
        self.scratch = unrealsdk.make_struct(SCRATCH_NAME_STRUCT)
        name_prop = next(p for p in self.scratch._type._properties() if str(p.Class.Name) == "NameProperty")
        self.address = int(self.scratch._get_address()) + int(name_prop.Offset_Internal)

    def resolve_raw(self, raw: bytes) -> str:
        index = i32(raw, 0)
        if index < 0 or index > 50_000_000:
            return f"<name {index}>"
        ctypes.memmove(self.address, raw, 8)
        try:
            return str(self.scratch.Name)
        except Exception as ex:  # noqa: BLE001
            return f"<name {index}: {type(ex).__name__}>"


def dump_run(views: DefView, names: NameResolver, by_address: dict[int, Any]) -> None:
    say(f"step 1: the run at {RUN_START:#x}")
    rarity_struct = find_struct(RARITY_STRUCT)
    block = read(RUN_START - 64, 64 + 12 * DEF_PTR_SIZE)
    if block is None:
        say("   unreadable")
        return
    say(f"   before: {hex_words(block[:64])}")
    for index in range(12):
        entry = block[64 + index * DEF_PTR_SIZE:64 + (index + 1) * DEF_PTR_SIZE]
        if len(entry) < DEF_PTR_SIZE:
            break
        struct = by_address.get(u64(entry, 8))
        resolved = u64(entry, 16)
        type_label = str(struct.Name) if struct is not None else f"{u64(entry, 8):#x}"
        label = f"name {names.resolve_raw(entry[:8])!r} type {type_label} resolved {resolved:#x}"
        if struct is rarity_struct and readable(resolved, 64):
            try:
                view = views.view(rarity_struct, resolved)
                label += f" -> {view.InternalName!s} / {view.DisplayName!s} sort {view.SortValue}"
            except Exception as ex:  # noqa: BLE001
                label += f" -> <err {type(ex).__name__}>"
        say(f"   [{index}] {label}")


def looks_like_globals(instance: int) -> bool:
    map_header = read(instance + GLOBALS_MAP_OFFSET, 16)
    order_header = read(instance + GLOBALS_RARITY_ORDER_OFFSET, 16)
    if map_header is None or order_header is None or len(map_header) < 16 or len(order_header) < 16:
        return False
    return is_pointer(u64(map_header, 0)) and 0 < i32(map_header, 8) <= 64 and u64(order_header, 0) == RUN_START


def search_window(needle: bytes) -> list[int]:
    """Addresses holding the needle within WINDOW_BYTES on each side of the run."""
    found: list[int] = []
    low, high = RUN_START - WINDOW_BYTES, RUN_START + WINDOW_BYTES
    for base, size, usable in regions(high):
        if not usable or base + size <= low:
            continue
        start = max(base, low)
        end = min(base + size, high)
        for chunk_start in range(start, end, CHUNK_BYTES):
            data = read(chunk_start, min(CHUNK_BYTES, end - chunk_start))
            if data is None:
                continue
            position = data.find(needle)
            while position >= 0:
                if position % 8 == 0:
                    found.append(chunk_start + position)
                position = data.find(needle, position + 1)
    return found


def dump_globals(views: DefView, names: NameResolver, by_address: dict[int, Any], instance: int) -> None:
    say(f"   globals at {instance:#x}")
    globals_struct = find_struct(GLOBALS_STRUCT)
    view = views.view(globals_struct, instance)
    for field in ("BaseWeaponTypeName", "BaseShieldTypeName", "BaseGadgetTypeName", "GrenadeGadgetTypeName", "RarityDefOrder", "Equipment", "WeaponPartAttributes"):
        try:
            say(f"      {field} = {described(getattr(view, field), 0, 3)}")
        except Exception as ex:  # noqa: BLE001
            say(f"      {field} = <err {type(ex).__name__}: {ex}>")
    try:
        say(f"      InventoryNamingAttributes (SDK) = {described(view.InventoryNamingAttributes, 0, 5)}")
    except Exception as ex:  # noqa: BLE001
        say(f"      InventoryNamingAttributes (SDK) = <err {type(ex).__name__}: {ex}>")
    raw = read(instance + GLOBALS_MAP_OFFSET, 80)
    data, num = u64(raw, 0), i32(raw, 8)
    say(f"      map header: data {data:#x} num {num}; raw {hex_words(raw)}")
    block = read(data, min(num, 16) * 96) if is_pointer(data) and 0 < num <= 64 else None
    if block is not None:
        say(f"      elements raw: {hex_words(block[:num * 48])}")
        for element_size in (24, 32, 40, 48, 56, 64, 72, 80, 96):
            if num * element_size > len(block):
                break
            keys = [names.resolve_raw(block[i * element_size:i * element_size + 8]) for i in range(num)]
            links = [(i32(block, (i + 1) * element_size - 8), i32(block, (i + 1) * element_size - 4)) for i in range(num)]
            plausible = all(-1 <= link[0] < num and 0 <= link[1] < 64 for link in links)
            say(f"      as {element_size}-byte elements{' (links fit)' if plausible else ''}: keys {keys}")
            if plausible:
                for i in range(num):
                    element = block[i * element_size:(i + 1) * element_size]
                    notes = [f"+8 as name {names.resolve_raw(element[8:16])!r}"]
                    for offset in range(8, element_size - 8, 8):
                        word = u64(element, offset)
                        struct = by_address.get(word)
                        if struct is not None:
                            notes.append(f"+{offset} type {struct.Name}, resolved {u64(element, offset + 8):#x}")
                        elif is_pointer(word):
                            text = fstring_at(word)
                            if text:
                                notes.append(f"+{offset} FString {text!r}")
                    say(f"         [{i}] {keys[i]!r}: {hex_words(element)} | {' | '.join(notes)}")
                break
    try:
        attributes = view.WeaponPartAttributes
        struct = attributes._type
        if struct is not None:
            say(f"      WeaponPartAttributes {def_label(attributes)} {struct._path_name()}: " + ", ".join(f"{p.Name}@{int(p.Offset_Internal)}:{str(p.Class.Name).replace('Property', '')}" for p in struct._properties()))
            for prop in struct._properties():
                say(f"         {prop.Name} = {described(getattr(attributes, str(prop.Name)), 0, 6)}")
    except Exception as ex:  # noqa: BLE001
        say(f"      WeaponPartAttributes: <err {type(ex).__name__}: {ex}>")


def background_scan(needle: bytes) -> None:
    started = time.time()
    headers: list[int] = []
    scanned = 0
    for base, size, usable in regions(LOW_HALF_LIMIT):
        if time.time() - started > TIME_BUDGET_S or len(headers) >= 8:
            break
        if not usable:
            continue
        for chunk_start in range(base, base + size, CHUNK_BYTES):
            data = read(chunk_start, min(CHUNK_BYTES, base + size - chunk_start))
            if data is None:
                continue
            scanned += len(data)
            position = data.find(needle)
            while position >= 0 and len(headers) < 8:
                if position % 8 == 0 and position + 16 <= len(data) and 0 < i32(data, position + 8) <= 32:
                    headers.append(chunk_start + position)
                    say(f"   header at {chunk_start + position:#x} (num {i32(data, position + 8)}) -> candidate {chunk_start + position - GLOBALS_RARITY_ORDER_OFFSET:#x}")
                position = data.find(needle, position + 1)
            time.sleep(0.002)
    RESULT.write_text(json.dumps({"globals": [], "rarity_runs": [f"{RUN_START:#x}"], "headers": [f"{h:#x}" for h in headers]}, indent=2), encoding="utf-8")
    say(f"background scan finished: {scanned / 1e9:.1f} GB in {time.time() - started:.0f} s, {len(headers)} header(s) -> {RESULT.name}; run probe_names22.py")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v23: the owner of the rarity run")
views: DefView | None = None
try:
    by_address = {int(s._get_address()): s for s in unrealsdk.find_all("ScriptStruct", exact=True)}
    views = DefView()
    names = NameResolver()
    dump_run(views, names, by_address)
    say("step 2: array headers pointing at the run, within the window")
    needle = RUN_START.to_bytes(8, "little")
    started = time.time()
    hits = search_window(needle)
    say(f"   {len(hits)} word(s) in {time.time() - started:.1f} s: {[f'{h:#x}' for h in hits[:16]]}")
    found = 0
    for hit in hits:
        header = read(hit, 16)
        if header is None or len(header) < 16:
            continue
        num, capacity = i32(header, 8), i32(header, 12)
        say(f"   header candidate {hit:#x}: num {num} max {capacity}")
        instance = hit - GLOBALS_RARITY_ORDER_OFFSET
        if 0 < num <= 32 and readable(instance, GLOBALS_SIZE) and looks_like_globals(instance):
            found += 1
            dump_globals(views, names, by_address, instance)
    if found == 0:
        say("   nothing in the window; scanning the low half of the address space in the background")
        threading.Thread(target=background_scan, args=(needle,), name="dmgnames23", daemon=True).start()
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
