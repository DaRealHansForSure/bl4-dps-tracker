"""
Phase 0 probe #34: attribute values on the live weapon, and the prefix table rows.

probe_names18 found `GbxAttributeBlueprintLibrary.GetValueOfAttribute(AttributeDef: Name,
ContextSource, Default)`: the game evaluates any attribute by name on a context object,
so an equipped weapon's actor can answer what the naming thresholds compare (the stat mod
parts only carry 1.0 "points", the real multipliers come out of the attribute system).
It also showed the prefix tables are Nexus config trees: rows of {field FName, value*,
(a, b)} entries. This probe:

1. lists every GbxAttributeDef the arena references (one scan), keeps the ones whose name
   mentions damage / fire rate / reload / accuracy / crit / magazine / elemental / ADS /
   naming, and evaluates each on the gun in hand;
2. prints the row structs of both prefix tables (their user-defined struct fields) and
   decodes every row entry: field name, type words, and the value as text / name / float.

    pyexec probe_names19.py    with the shotgun in hand

Raw reads are checked against VirtualQuery; the only game code run is the Blueprint
attribute getter, with the weapon actor as context. No hooks.
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
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
STRATEGY_STRUCT = "/Script/GbxGame.InventoryNamingStrategyDef"
ATTRIBUTE_STRUCT = "/Script/GbxEngine.GbxAttributeDef"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
SCRATCH_NAME_STRUCT = "InventoryPartCriterionData"
MAX_TRIPLES = 60000
MAX_EVALUATIONS = 400
DEFAULT_SENTINEL = -12345.0
KEYWORDS = ("damage", "fire_rate", "firerate", "reload", "accuracy", "crit", "mag", "elemental", "proficiency", "ads", "naming", "spread", "handling")
ROW_ENTRY_SIZE = 24
ROW_TABLE_OFFSET = 80
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


def say(msg: str) -> None:
    logging.info(f"[dmgnames19] {msg}")
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


def f32(data: bytes, offset: int) -> float:
    return ctypes.c_float.from_buffer_copy(data[offset:offset + 4]).value


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
    if header is None:
        return None
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    if num <= 1 or num > MAX_TEXT_CHARS or capacity < num or capacity > 4096:
        return None
    raw = read(data, num * 2)
    if raw is None or raw[-2:] != b"\x00\x00":
        return None
    try:
        text = raw[:-2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not text or not all(32 <= ord(ch) < 0x3000 for ch in text):
        return None
    return text


def display_string(text_data: int, depth: int = 0) -> str | None:
    body = read(text_data, 96)
    if body is None:
        return None
    for offset in range(0, 96 - 16 + 1, 8):
        found = fstring_at(text_data + offset)
        if found:
            return found
    if depth >= 2:
        return None
    for offset in range(0, 96 - 8 + 1, 8):
        word = u64(body, offset)
        if is_pointer(word) and word != text_data:
            found = fstring_at(word)
            if found:
                return found
            found = display_string(word, depth + 1)
            if found:
                return found
    return None


def read_ftext(address: int) -> str | None:
    header = read(address, 16)
    if header is None:
        return None
    return display_string(u64(header, 0))


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
        self.cache: dict[bytes, str] = {}

    def resolve_raw(self, raw: bytes) -> str:
        cached = self.cache.get(raw)
        if cached is not None:
            return cached
        index = i32(raw, 0)
        if index < 0 or index > 50_000_000:
            text = f"<name {index}>"
        else:
            ctypes.memmove(self.address, raw, 8)
            try:
                text = str(self.scratch.Name)
            except Exception as ex:  # noqa: BLE001
                text = f"<name {index}: {type(ex).__name__}>"
        self.cache[raw] = text
        return text


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

    def find_all(self, needle: bytes, aligned: int = 1, limit: int = 64) -> list[int]:
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


def attribute_names(arena: Arena, names: NameResolver) -> dict[str, int]:
    """Attribute def name -> data address, from every GbxAttributeDef triple in the arena."""
    struct = find_struct(ATTRIBUTE_STRUCT)
    hits = arena.find_all(int(struct._get_address()).to_bytes(8, "little"), 8, MAX_TRIPLES)
    found: dict[str, int] = {}
    for hit in hits:
        raw = read(hit - 8, DEF_PTR_SIZE)
        if raw is None:
            continue
        resolved = u64(raw, 16)
        if not is_pointer(resolved):
            continue
        name = names.resolve_raw(raw[:8])
        if name not in found:
            found[name] = resolved
    say(f"   {len(hits)} attribute def pointer(s), {len(found)} distinct name(s)")
    return found


def evaluate_attributes(weapon: UObject, candidates: list[str]) -> None:
    library = unrealsdk.find_class("GbxAttributeBlueprintLibrary").ClassDefaultObject
    say(f"   evaluating {len(candidates)} attribute(s) on {weapon._path_name()}")
    for name in candidates[:MAX_EVALUATIONS]:
        try:
            value = library.GetValueOfAttribute(name, weapon, DEFAULT_SENTINEL)
        except Exception as ex:  # noqa: BLE001
            say(f"      {name}: <err {type(ex).__name__}: {ex}>")
            continue
        if value == DEFAULT_SENTINEL:
            continue
        say(f"      {name} = {value}")


def decode_value(pointer: int, kind_a: int, kind_b: int, names: NameResolver) -> str:
    raw = read(pointer, 64)
    if raw is None:
        return "unreadable"
    guesses = [f"raw {hex_words(raw[:32])}"]
    text = read_ftext(pointer)
    if text:
        guesses.append(f"as FText {text!r}")
    string = fstring_at(pointer)
    if string:
        guesses.append(f"as FString {string!r}")
    guesses.append(f"as Name {names.resolve_raw(raw[:8])!r}")
    guesses.append(f"as float {f32(raw, 0):.4g} / int {i32(raw, 0)}")
    return "; ".join(guesses)


def dump_rows(label: str, table_address: int, names: NameResolver, row_struct_path: str) -> None:
    say(f"   {label} at {table_address:#x}")
    try:
        row_struct = unrealsdk.find_object("UserDefinedStruct", row_struct_path)
        say("      row struct fields: " + ", ".join(f"{p.Name}:{str(p.Class.Name).replace('Property', '')}@{int(p.Offset_Internal)}" for p in row_struct._properties()))
    except Exception as ex:  # noqa: BLE001
        say(f"      row struct: <err {type(ex).__name__}: {ex}>")
    raw = read(table_address, 160)
    if raw is None:
        say("      table unreadable")
        return
    data, num = u64(raw, ROW_TABLE_OFFSET), i32(raw, ROW_TABLE_OFFSET + 8)
    if not is_pointer(data) or num <= 0 or num > 64:
        say("      no row storage")
        return
    entries_per_row = None
    block = read(data, num * 8 * ROW_ENTRY_SIZE)
    if block is None:
        say("      row storage unreadable")
        return
    # Rows start with the same header key; count entries until it repeats.
    first_key = u64(block, 0)
    for stride in range(1, 9):
        if u64(block, stride * ROW_ENTRY_SIZE) == first_key:
            entries_per_row = stride
            break
    say(f"      {num} row(s), header key {first_key:#x}, {entries_per_row} entries per row")
    if entries_per_row is None:
        say(f"      first entries: {hex_words(block[:8 * ROW_ENTRY_SIZE])}")
        return
    for row in range(num):
        say(f"      row {row}:")
        for entry in range(entries_per_row):
            offset = (row * entries_per_row + entry) * ROW_ENTRY_SIZE
            if offset + ROW_ENTRY_SIZE > len(block):
                break
            key_raw = block[offset:offset + 8]
            pointer, kind_a, kind_b = u64(block, offset + 8), i32(block, offset + 16), i32(block, offset + 20)
            key = names.resolve_raw(key_raw) if u64(key_raw, 0) != first_key else f"header {first_key:#x}"
            say(f"         {key}: ({kind_a}, {kind_b}) -> {decode_value(pointer, kind_a, kind_b, names) if is_pointer(pointer) else pointer}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v19: attribute values and prefix rows")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
views: DefView | None = None
try:
    views = DefView()
    names = NameResolver()
    if weapon is None:
        say("no weapon in hand")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        head0 = u64(read(identity, 8), 0)
        type_struct = find_struct(TYPE_STRUCT)
        type_view = views.view(type_struct, head0)
        say(f"   gun: {type_view.ItemBaseName!s}; actor {weapon._path_name()}")
        strategy = views.view(find_struct(STRATEGY_STRUCT), int(type_view.NamingStrategyDef._get_address())).NamingStrategy
        tables = []
        for field in ("PayloadDeliveryDataTable", "MagBarrelDataTable"):
            pointer = getattr(strategy, field)
            path = ""
            try:
                path = str(pointer.RowStructPath._path_name()) if pointer.RowStructPath is not None else ""
            except Exception:  # noqa: BLE001
                try:
                    path = str(pointer.RowStructPath)
                except Exception:  # noqa: BLE001
                    path = ""
            tables.append((field, int(pointer._get_address()), path))
        say("step 1: attribute values on the gun")
        arena = Arena(head0)
        say(f"   arena at {arena.base:#x}: {arena.size / 1e6:.1f} MB")
        attributes = attribute_names(arena, names)
        candidates = sorted(name for name in attributes if any(keyword in name.lower() for keyword in KEYWORDS))
        say(f"   {len(candidates)} candidate name(s): {candidates}")
        evaluate_attributes(weapon, candidates)
        say("step 2: the prefix table rows")
        for field, address, path in tables:
            dump_rows(field, address, names, path)
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
