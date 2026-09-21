"""
Phase 0 probe #30: every name part in the arena, and who owns the prefix pools.

probe_names14 proved the forged def pointer: the SDK types the data by the triple's type
word, so any definition address can be read through reflection. This probe uses that to:

1. list every InventoryNamePartDef the arena references (one scan for the struct address),
   reading PartName / priority / game stage / rarity bounds through the typed view;
2. for the words of the four known gun names, print each referencing triple, walk back to
   the start of its array, find the array header (one scan per array, at most 14) and so
   the owning record and the field offset inside it, plus that record's own header;
3. read the naming strategy through the typed view, including the inline strategy struct,
   and resolve the aspect type ids seen in probe 8 as FNames.

    pyexec probe_names15.py    with the shotgun in hand

Every raw read is checked against VirtualQuery first. Only scratch memory the SDK gave us
is written. No hooks, no calls into the game.
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
from unrealsdk.unreal import FGbxDefPtr, UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
MAX_ARENA_BYTES = 512 * 1024 * 1024
DEF_PTR_SIZE = 24
DEF_HEADER_SIZE = 0x18
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
NAME_PART_STRUCT = "/Script/GbxGame.InventoryNamePartDef"
STRATEGY_STRUCT = "/Script/GbxGame.InventoryNamingStrategyDef"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
SCRATCH_NAME_STRUCT = "InventoryPartCriterionData"
MAX_VALUE_CHARS = 400
MAX_ITEMS = 40
MAX_TRIPLES = 20000
MAX_ARRAY_SCANS = 14
KNOWN_WORDS = (
    "Maintained", "Screeching", "T.K's Wave",
    "Lionized", "Parasite",
    "Inoculated", "Performative", "Handcannon",
    "Domino", "Toochney", "Lucian's Flank",
)
ASPECT_TYPE_IDS = (19076, 19401, 45783, 45778, 45803, 45804, 45915, 46091, 46094, 46199, 108634, 108282)


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
    logging.info(f"[dmgnames15] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def region(address: int) -> MemoryBasicInformation | None:
    if address < POINTER_MIN or address > POINTER_MAX:
        return None
    info = MemoryBasicInformation()
    if kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
        return None
    return info


def region_readable(info: MemoryBasicInformation) -> bool:
    if info.State != MEM_COMMIT or info.Protect & PAGE_GUARD or info.Protect & PAGE_NOACCESS:
        return False
    return any(info.Protect & flag for flag in READABLE_PROTECTIONS)


def readable(address: int, size: int) -> bool:
    if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
        return False
    cursor = address
    end = address + size
    while cursor < end:
        info = region(cursor)
        if info is None or not region_readable(info):
            return False
        cursor = (info.BaseAddress or 0) + info.RegionSize
    return True


def read(address: int, size: int) -> bytes | None:
    if not readable(address, size):
        return None
    return ctypes.string_at(address, size)


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


def described(value: Any, depth: int = 0) -> str:
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, FGbxDefPtr):
            try:
                return f"<def at {int(value._get_address()):#x}>"
            except Exception:  # noqa: BLE001
                return "<def>"
        if isinstance(value, WrappedStruct):
            if depth >= 2:
                return f"<struct {value._type.Name}>"
            parts = []
            for prop in value._type._properties():
                name = str(prop.Name)
                try:
                    parts.append(f"{name}={described(getattr(value, name), depth + 1)}")
                except Exception as ex:  # noqa: BLE001
                    parts.append(f"{name}=<err {type(ex).__name__}>")
            return "{" + ", ".join(parts) + "}"
        if isinstance(value, WrappedArray):
            items = [described(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
            more = "" if len(value) <= MAX_ITEMS else f", ... {len(value)} items"
            return "[" + ", ".join(items) + more + "]"
        text = repr(value)
        return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"


class DefView:
    """Typed access to a definition instance through a forged def pointer in scratch memory."""

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
    """Turns raw FName indices into strings through a scratch struct the SDK owns."""

    def __init__(self) -> None:
        self.scratch = unrealsdk.make_struct(SCRATCH_NAME_STRUCT)
        name_prop = next(p for p in self.scratch._type._properties() if str(p.Class.Name) == "NameProperty")
        self.address = int(self.scratch._get_address()) + int(name_prop.Offset_Internal)

    def resolve(self, index: int, number: int = 0) -> str:
        if index < 0 or index > 50_000_000:
            return f"<name {index}>"
        raw = index.to_bytes(4, "little") + (number & 0xFFFFFFFF).to_bytes(4, "little")
        ctypes.memmove(self.address, raw, 8)
        try:
            return str(self.scratch.Name)
        except Exception as ex:  # noqa: BLE001
            return f"<name {index}: {type(ex).__name__}>"


class Arena:
    def __init__(self, sample_pointer: int) -> None:
        info = region(sample_pointer)
        if info is None:
            raise RuntimeError("no region")
        base = info.AllocationBase or info.BaseAddress or 0
        chunks: list[tuple[int, bytes]] = []
        cursor = base
        total = 0
        while total < MAX_ARENA_BYTES:
            part = region(cursor)
            if part is None or (part.AllocationBase or 0) != base:
                break
            size = part.RegionSize
            if region_readable(part) and size <= MAX_ARENA_BYTES:
                chunks.append((cursor, ctypes.string_at(cursor, size)))
                total += size
            cursor += size
        self.base = base
        self.chunks = chunks
        self.size = total
        self.scans = 0

    def find_all(self, needle: bytes, aligned: int = 1, limit: int = 64) -> list[int]:
        self.scans += 1
        hits: list[int] = []
        for start, data in self.chunks:
            position = data.find(needle)
            while position >= 0 and len(hits) < limit:
                if (start + position) % aligned == 0:
                    hits.append(start + position)
                position = data.find(needle, position + 1)
            if len(hits) >= limit:
                break
        return hits

    def pointers_to(self, address: int, limit: int = 64) -> list[int]:
        return self.find_all(address.to_bytes(8, "little"), 8, limit)

    def contains(self, address: int) -> bool:
        return self.base <= address < self.base + self.size


def part_summary(views: DefView, part_struct: Any, address: int) -> tuple[str, str]:
    """(PartName, details) of the name part at address through the typed view."""
    part = views.view(part_struct, address)
    try:
        text = str(part.PartName)
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}>", ""
    details = ""
    try:
        details = f"priority {part.priority} stages {part.mingamestage}-{part.MaxGameStage} rarity {described(part.MinRarity)}..{described(part.MaxRarity)} manufacturer {described(part.RequiredManufacturer)}"
    except Exception as ex:  # noqa: BLE001
        details = f"<err {type(ex).__name__}>"
    return text, details


def owner_of_triple(arena: Arena, triple: int, part_struct_address: int) -> None:
    """Walk back to the array start, find its header and report the owning record."""
    start = triple
    while True:
        previous = read(start - DEF_PTR_SIZE, DEF_PTR_SIZE)
        if previous is None or u64(previous, 8) != part_struct_address:
            break
        start -= DEF_PTR_SIZE
    count = (triple - start) // DEF_PTR_SIZE
    if arena.scans >= MAX_ARRAY_SCANS + 1:
        say(f"         array starts at {start:#x} (entry {count}); scan budget spent, owner not looked up")
        return
    headers = arena.pointers_to(start, 8)
    if not headers:
        say(f"         array starts at {start:#x} (entry {count}); no header in the arena")
        return
    for header in headers:
        header_raw = read(header, 16)
        if header_raw is None:
            continue
        num, capacity = i32(header_raw, 8), i32(header_raw, 12)
        if not (0 < num <= 64 and num <= capacity <= 256):
            continue
        say(f"         array starts at {start:#x} (entry {count} of {num}); header at {header:#x}")
        # The record that holds the header: its own 24-byte def header sits before the data.
        for back in (56, 72, 88, 368, 384, 400):
            record = header - back
            record_header = read(record - DEF_HEADER_SIZE, DEF_HEADER_SIZE)
            if record_header is None:
                continue
            first, size_word = u64(record_header, 0), u64(record_header, 16)
            if size_word == DEF_HEADER_SIZE and is_pointer(first):
                say(f"            as a record with the list at +{back}: record {record:#x}, header {hex_words(record_header)}")
                heap = read(first, 64)
                if heap is not None:
                    say(f"            header record at {first:#x}: {hex_words(heap)}")
        context = read(header - 96, 96 + 48)
        if context is not None:
            say(f"            around the header: {hex_words(context)}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v15: name parts and their owners")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
views: DefView | None = None
try:
    by_address = {int(s._get_address()): s for s in unrealsdk.find_all("ScriptStruct", exact=True)}
    type_struct = find_struct(TYPE_STRUCT)
    part_struct = find_struct(NAME_PART_STRUCT)
    strategy_struct = find_struct(STRATEGY_STRUCT)
    views = DefView()
    names = NameResolver()
    if weapon is None:
        say("no weapon in hand")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        head0 = u64(read(identity, 8), 0)
        type_view = views.view(type_struct, head0)
        say(f"   gun: {type_view.ItemBaseName!s}")
        arena = Arena(head0)
        say(f"   arena at {arena.base:#x}: {arena.size / 1e6:.1f} MB")

        say("step 1: every name part the arena references")
        part_struct_address = int(part_struct._get_address())
        hits = arena.find_all(part_struct_address.to_bytes(8, "little"), 8, MAX_TRIPLES)
        triples_by_part: dict[int, list[int]] = {}
        for hit in hits:
            raw = read(hit - 8, DEF_PTR_SIZE)
            if raw is None:
                continue
            resolved = u64(raw, 16)
            if is_pointer(resolved) and readable(resolved, 192):
                triples_by_part.setdefault(resolved, []).append(hit - 8)
        say(f"   {len(hits)} type word(s), {len(triples_by_part)} distinct name part(s)")
        texts: dict[int, tuple[str, str]] = {}
        started = time.time()
        for address in triples_by_part:
            texts[address] = part_summary(views, part_struct, address)
        say(f"   read {len(texts)} part name(s) in {time.time() - started:.1f} s")
        counted: dict[str, int] = {}
        for text, _details in texts.values():
            counted[text] = counted.get(text, 0) + 1
        sample = sorted(counted.items(), key=lambda item: -item[1])[:60]
        say("   most referenced words: " + ", ".join(f"{text!r} x{count}" for text, count in sample))

        say("step 2: the known words and their owners")
        for word in KNOWN_WORDS:
            matches = [address for address, (text, _d) in texts.items() if text == word]
            say(f"   {word!r}: {len(matches)} part(s)")
            for address in matches[:3]:
                text, details = texts[address]
                say(f"      part {address:#x}: {details}; referenced from {len(triples_by_part[address])} triple(s)")
                for triple in triples_by_part[address][:3]:
                    owner_of_triple(arena, triple, part_struct_address)

        say("step 3: the naming strategy")
        strategy_address = int(type_view.NamingStrategyDef._get_address())
        strategy = views.view(strategy_struct, strategy_address)
        try:
            inline = strategy.NamingStrategy
            say(f"   NamingStrategy through the SDK: {type(inline).__name__} {described(inline)}")
            try:
                say(f"   inline type {inline._type._path_name()}: " + ", ".join(f"{p.Name}@{int(p.Offset_Internal)}:{str(p.Class.Name).replace('Property', '')}" for p in inline._type._properties()))
            except Exception as ex:  # noqa: BLE001
                say(f"   inline type unavailable: {type(ex).__name__}: {ex}")
        except Exception as ex:  # noqa: BLE001
            say(f"   NamingStrategy through the SDK failed: {type(ex).__name__}: {ex}")
        raw = read(strategy_address, 48)
        if raw is not None:
            say(f"   strategy def raw: {hex_words(raw)}")
            record_a = u64(raw, 24)
            if is_pointer(record_a):
                data = read(record_a - DEF_HEADER_SIZE, DEF_HEADER_SIZE + 128)
                if data is not None:
                    say(f"   inline record header+data at {record_a - DEF_HEADER_SIZE:#x}: {hex_words(data)}")
                    heap = read(u64(data, 0), 96)
                    if heap is not None:
                        say(f"   header's heap record: {hex_words(heap)}")
                        for offset in range(0, 96 - 7, 8):
                            struct = by_address.get(u64(heap, offset))
                            if struct is not None:
                                say(f"      +{offset}: struct {struct._path_name()}")
        say("step 4: aspect type ids as FNames")
        say("   " + ", ".join(f"{index}={names.resolve(index)!r}" for index in ASPECT_TYPE_IDS))
        say(f"   arena scans used: {arena.scans}")
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
