"""
Phase 0 probe #28: the inventory globals and their naming attributes.

`OakGlobalsInventory` (a Nexus globals definition) carries `InventoryNamingAttributes: Map`,
which suggests the prefixes are chosen from the item's attributes (its stat modifiers)
rather than from the parts. This probe, without any process-wide scan:

1. prints the layouts of OakGlobalsInventory / GbxGlobalsInventory, with the map's key and
   value types where the SDK exposes them;
2. snapshots the definition arena (the region holding the gun's type definition) and finds
   every 24-byte definition pointer whose type word is the OakGlobalsInventory struct, so the
   third word is the globals instance;
3. dumps the globals instance field by field, the naming attributes map raw and parsed, and
   resolves FName indices through a scratch struct the SDK owns;
4. reads the gun's manufacturer, composition and rarity definitions with their real layouts.

    pyexec probe_names13.py    with the gun in hand

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
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MAX_TEXT_CHARS = 200
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
MAX_ARENA_BYTES = 512 * 1024 * 1024
DEF_PTR_SIZE = 24
GLOBALS_STRUCT = "/Script/OakGame.OakGlobalsInventory"
BASE_GLOBALS_STRUCT = "/Script/GbxGame.GbxGlobalsInventory"
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
NAME_PART_STRUCT = "/Script/GbxGame.InventoryNamePartDef"
SCRATCH_NAME_STRUCT = "/Script/GbxGame.InventoryPartCriterionData"
MAX_MAP_ELEMENTS = 64
MAX_LIST_LINES = 48


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
    logging.info(f"[dmgnames13] {msg}")
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


def u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


def i32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def f32(data: bytes, offset: int) -> float:
    return ctypes.c_float.from_buffer_copy(data[offset:offset + 4]).value


def is_pointer(word: int) -> bool:
    return POINTER_MIN <= word <= POINTER_MAX


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


def read_tarray(address: int, element_size: int, max_elements: int = 64) -> list[bytes]:
    header = read(address, 16)
    if header is None:
        return []
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    if num <= 0 or num > max_elements or capacity < num or capacity > 65536:
        return []
    raw = read(data, num * element_size)
    if raw is None:
        return []
    return [raw[i * element_size:(i + 1) * element_size] for i in range(num)]


def find_struct(path: str) -> Any:
    try:
        return unrealsdk.find_object("ScriptStruct", path)
    except ValueError:
        wanted = path.rsplit(".", 1)[-1]
        for struct in unrealsdk.find_all("ScriptStruct", exact=True):
            if str(struct.Name) == wanted:
                return struct
        raise


def props_of(struct: Any) -> list[Any]:
    return list(struct._properties())


def kind_of(prop: Any) -> str:
    return str(prop.Class.Name).replace("Property", "")


def inner_types(prop: Any) -> str:
    """Key/value or inner types of a container property, where the SDK exposes them."""
    parts: list[str] = []
    for attribute in ("Inner", "KeyProp", "ValueProp", "ElementProp"):
        try:
            inner = getattr(prop, attribute)
        except Exception:  # noqa: BLE001
            continue
        if inner is None:
            continue
        description = f"{attribute}={kind_of(inner)}"
        try:
            description += f"({int(inner.ElementSize)}B)"
        except Exception:  # noqa: BLE001
            pass
        for detail in ("Struct", "PropertyClass", "Enum"):
            try:
                target = getattr(inner, detail)
                if target is not None:
                    description += f"<{target.Name}>"
            except Exception:  # noqa: BLE001
                pass
        parts.append(description)
    return " ".join(parts)


class NameResolver:
    """Turns raw FName indices into strings through a scratch struct the SDK owns."""

    def __init__(self) -> None:
        self.scratch = unrealsdk.make_struct(SCRATCH_NAME_STRUCT.rsplit(".", 1)[-1])
        name_prop = next(p for p in find_struct(SCRATCH_NAME_STRUCT)._properties() if kind_of(p) == "Name")
        self.address = int(self.scratch._get_address()) + int(name_prop.Offset_Internal)
        self.cache: dict[int, str] = {}

    def resolve(self, index: int, number: int = 0) -> str:
        key = (index << 32) | (number & 0xFFFFFFFF)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if index < 0 or index > 50_000_000:
            return f"<name {index}>"
        raw = index.to_bytes(4, "little", signed=False) + (number & 0xFFFFFFFF).to_bytes(4, "little")
        ctypes.memmove(self.address, raw, 8)
        try:
            text = str(self.scratch.Name)
        except Exception as ex:  # noqa: BLE001
            text = f"<name {index}: {type(ex).__name__}>"
        self.cache[key] = text
        return text

    def fname_at(self, data: bytes, offset: int) -> str:
        return self.resolve(i32(data, offset), i32(data, offset + 4))


class Arena:
    """One snapshot of the definition region around a known definition pointer."""

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

    def contains(self, address: int) -> bool:
        return self.base <= address < self.base + self.size


def describe_layout(path: str) -> None:
    struct = find_struct(path)
    say(f"   {path} ({struct._get_struct_size()} bytes, super {struct.SuperField.Name if struct.SuperField else None})")
    for prop in props_of(struct):
        say(f"      {prop.Name}@{int(prop.Offset_Internal)} {kind_of(prop)} {int(prop.ElementSize)}B {inner_types(prop)}")


def describe_value(base: int, prop: Any, names: NameResolver, indent: str) -> None:
    kind = kind_of(prop)
    address = base + int(prop.Offset_Internal)
    if kind == "Text":
        say(f"{indent}{prop.Name}: Text {read_ftext(address)!r}")
    elif kind == "Str":
        say(f"{indent}{prop.Name}: Str {fstring_at(address)!r}")
    elif kind == "Name":
        raw = read(address, 8)
        say(f"{indent}{prop.Name}: Name {names.fname_at(raw, 0) if raw else '?'}")
    elif kind in ("Int", "UInt32", "Int16", "Byte", "Int64", "UInt64"):
        raw = read(address, 8)
        say(f"{indent}{prop.Name}: {kind} {i32(raw, 0) if raw else '?'}")
    elif kind == "Float":
        raw = read(address, 4)
        say(f"{indent}{prop.Name}: Float {f32(raw, 0) if raw else '?'}")
    elif kind == "Bool":
        raw = read(address, 1)
        say(f"{indent}{prop.Name}: Bool {bool(raw[0]) if raw else '?'}")
    elif kind == "GbxDefPtr":
        raw = read(address, DEF_PTR_SIZE)
        if raw:
            say(f"{indent}{prop.Name}: DefPtr name {u64(raw, 0):#x} type {u64(raw, 8):#x} resolved {u64(raw, 16):#x}")
    elif kind == "Array":
        raw = read(address, 16)
        if raw:
            say(f"{indent}{prop.Name}: Array data {u64(raw, 0):#x} num {i32(raw, 8)} max {i32(raw, 12)} {inner_types(prop)}")
    elif kind == "Map":
        raw = read(address, int(prop.ElementSize))
        if raw:
            say(f"{indent}{prop.Name}: Map {inner_types(prop)} raw " + " ".join(raw[i:i + 8].hex() for i in range(0, len(raw), 8)))
    else:
        say(f"{indent}{prop.Name}: {kind} @{int(prop.Offset_Internal)}")


def dump_map_elements(address: int, prop: Any, names: NameResolver, by_address: dict[int, Any]) -> None:
    """The elements of a TMap at address: raw pairs, with any def pointers and names explained."""
    header = read(address, 16)
    if header is None:
        say("      map header unreadable")
        return
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    say(f"      elements data {data:#x} num {num} max {capacity}")
    if not is_pointer(data) or num <= 0 or num > 4096:
        return
    key_size = value_size = 0
    try:
        key_size = int(prop.KeyProp.ElementSize)
        value_size = int(prop.ValueProp.ElementSize)
    except Exception:  # noqa: BLE001
        pass
    pair_size = key_size + value_size
    if pair_size == 0:
        # Unknown layout: show the first 512 bytes and let the reader place the fields.
        raw = read(data, 512)
        if raw:
            say("      raw: " + " ".join(raw[i:i + 8].hex() for i in range(0, len(raw), 8)))
        return
    pair_size = (pair_size + 7) & ~7
    element_size = pair_size + 8  # HashNextId, HashIndex
    raw = read(data, min(num, MAX_MAP_ELEMENTS) * element_size)
    if raw is None:
        say("      elements unreadable")
        return
    say(f"      key {key_size}B value {value_size}B -> element {element_size}B")
    for index in range(min(num, MAX_MAP_ELEMENTS)):
        element = raw[index * element_size:(index + 1) * element_size]
        words = " ".join(element[i:i + 8].hex() for i in range(0, len(element), 8))
        notes: list[str] = []
        for offset in range(0, len(element) - 7, 8):
            word = u64(element, offset)
            struct = by_address.get(word)
            if struct is not None:
                notes.append(f"+{offset} type {struct.Name}")
        key_kind = kind_of(prop.KeyProp) if key_size else "?"
        if key_kind == "Name":
            notes.append(f"key name {names.fname_at(element, 0)!r}")
        if key_kind == "GbxDefPtr":
            notes.append(f"key resolved {u64(element, 16):#x}")
        say(f"      [{index}] {words} {' | '.join(notes)}")


def dump_instance(path: str, base: int, names: NameResolver, by_address: dict[int, Any]) -> None:
    struct = find_struct(path)
    for prop in props_of(struct):
        describe_value(base, prop, names, "      ")
        if kind_of(prop) == "Map":
            dump_map_elements(base + int(prop.Offset_Internal), prop, names, by_address)


def find_globals(arena: Arena, struct: Any) -> list[int]:
    """Instances of the struct: third words of arena def pointers whose type word is the struct."""
    type_address = int(struct._get_address())
    hits = arena.find_all(type_address.to_bytes(8, "little"), 8, 256)
    say(f"   {len(hits)} word(s) in the arena equal the {struct.Name} struct address")
    instances: list[int] = []
    for hit in hits:
        raw = read(hit - 8, 24)
        if raw is None:
            continue
        name_word, resolved = u64(raw, 0), u64(raw, 16)
        if resolved and readable(resolved, 64):
            say(f"      def pointer at {hit - 8:#x}: name word {name_word:#x}, resolved {resolved:#x} ({'arena' if arena.contains(resolved) else 'elsewhere'})")
            if resolved not in instances:
                instances.append(resolved)
        else:
            say(f"      word at {hit:#x}: neighbours {u64(raw, 0):#x} / {resolved:#x} (not a def pointer)")
    return instances


def dump_gun_defs(head: int, head1: int, names: NameResolver, by_address: dict[int, Any]) -> None:
    say("step 4: the gun's manufacturer, composition and rarity definitions")
    type_offsets = {str(p.Name): int(p.Offset_Internal) for p in props_of(find_struct(TYPE_STRUCT))}
    for field, struct_path in (("Manufacturer", "/Script/OakGame.ManufacturerDef"), ("BaseType", TYPE_STRUCT)):
        raw = read(head + type_offsets[field], DEF_PTR_SIZE)
        if raw is None:
            continue
        resolved = u64(raw, 16)
        struct = by_address.get(u64(raw, 8))
        say(f"   {field}: type {struct.Name if struct is not None else u64(raw, 8):#x}, resolved {resolved:#x}")
        if resolved and readable(resolved, 64):
            try:
                target = find_struct(struct_path) if struct is None else struct
                for prop in props_of(target):
                    if kind_of(prop) in ("Text", "Str", "Name", "Int", "Bool", "GbxDefPtr", "Array"):
                        describe_value(resolved, prop, names, "      ")
            except Exception as ex:  # noqa: BLE001
                say(f"      failed: {type(ex).__name__}: {ex}")
    if head1:
        raw = read(head1, 128)
        if raw:
            for offset in (72, 96):
                struct = by_address.get(u64(raw, offset + 8))
                resolved = u64(raw, offset + 16)
                if struct is None or not readable(resolved, 64):
                    continue
                say(f"   second head +{offset}: {struct.Name} at {resolved:#x}")
                for prop in props_of(struct):
                    if kind_of(prop) in ("Text", "Str", "Name", "Int", "Bool", "GbxDefPtr"):
                        describe_value(resolved, prop, names, "      ")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v13: the inventory globals")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
try:
    names = NameResolver()
    say(f"   name resolver check: 0 -> {names.resolve(0)!r}, 1 -> {names.resolve(1)!r}")
    by_address = {int(s._get_address()): s for s in unrealsdk.find_all("ScriptStruct", exact=True)}
    say("step 1: layouts")
    describe_layout(BASE_GLOBALS_STRUCT)
    describe_layout(GLOBALS_STRUCT)
    if weapon is None:
        say("no weapon in hand; the arena needs a definition pointer")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        heads = read(identity, 16)
        head0, head1 = u64(heads, 0), u64(heads, 8)
        say(f"step 2: the arena around {head0:#x}")
        arena = Arena(head0)
        say(f"   arena at {arena.base:#x}: {arena.size / 1e6:.1f} MB in {len(arena.chunks)} chunk(s)")
        instances = find_globals(arena, find_struct(GLOBALS_STRUCT))
        say("step 3: the globals instance(s)")
        for instance in instances[:4]:
            say(f"   instance {instance:#x}")
            dump_instance(GLOBALS_STRUCT, instance, names, by_address)
        dump_gun_defs(head0, head1, names, by_address)
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
say("done")
