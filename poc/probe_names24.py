"""
Phase 0 probe #39: attribute definitions, the weapon's attribute properties, and tags.

Two questions remain for composing a name: how the naming multipliers (damage 1.2 ...) come
out of the parts, and how the game tells a licensed part (Borg magazine -> "Maintained").
This probe collects the raw material for both:

1. the GbxAttributeDef layout and the typed definitions of the weapon attributes the stats
   definition names (weapon_damage, weapon_fire_rate, weapon_reload_time, weapon_spread,
   accuracy_resource_max_value, ...): their initializers say where base values come from;
2. every GbxAttributeFloat / GbxAttributeInteger property of the weapon actor and its
   components, with Value and BaseValue;
3. the criterion tag names of the inventory config store (GetCriterionTagNames), to see
   whether borg_mag / tor_mag / cov_mag / jak_barrel_acc / hyp_shield / licensed_ted are
   tags;
4. the raw hidden bytes of every part definition (offsets 0..88) and the RequiredBaseTags
   of the name parts that have any, to learn the InventoryTags encoding.

    pyexec probe_names24.py    with the shotgun in hand

Raw reads go through ReadProcessMemory. The only game code run is GetCriterionTagNames on
the config store. No hooks.
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
PART_STRUCT = "/Script/GbxGame.InventoryPartDef"
NAME_PART_STRUCT = "/Script/GbxGame.InventoryNamePartDef"
ATTRIBUTE_STRUCT = "/Script/GbxEngine.GbxAttributeDef"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
SCRATCH_NAME_STRUCT = "InventoryPartCriterionData"
MAX_VALUE_CHARS = 2500
MAX_ITEMS = 64
WANTED_ATTRIBUTES = (
    "weapon_damage", "weapon_fire_rate", "weapon_reload_time", "weapon_spread", "accuracy_resource_max_value",
    "weapon_accuracy_impulse", "weapon_damage_modifier_add_critical_hit", "weapon_burst_fire_delay", "weapon_charge_time",
)
MAX_PART_TRIPLES = 20000


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
    logging.info(f"[dmgnames24] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def read(address: int, size: int) -> bytes | None:
    if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(PROCESS, ctypes.c_void_p(address), buffer, size, ctypes.byref(got))
    if (not ok and got.value == 0) or got.value < size:
        return None
    return buffer.raw


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


def describe_layout(path: str) -> Any:
    struct = find_struct(path)
    say(f"   {path} ({struct._get_struct_size()} bytes): " + ", ".join(f"{p.Name}@{int(p.Offset_Internal)}:{str(p.Class.Name).replace('Property', '')}" for p in struct._properties()))
    return struct


def dump_attribute_defs(views: DefView, names: NameResolver, arena: Arena) -> None:
    say("step 1: the weapon attribute definitions")
    struct = describe_layout(ATTRIBUTE_STRUCT)
    hits = arena.find_all(int(struct._get_address()).to_bytes(8, "little"), 8, MAX_PART_TRIPLES)
    wanted = set(WANTED_ATTRIBUTES)
    seen: set[str] = set()
    for hit in hits:
        raw = read(hit - 8, DEF_PTR_SIZE)
        if raw is None:
            continue
        name = names.resolve_raw(raw[:8])
        resolved = u64(raw, 16)
        if name in wanted and name not in seen and is_pointer(resolved):
            seen.add(name)
            view = views.view(struct, resolved)
            say(f"   {name} at {resolved:#x}: {described(view, 0, 5)}")
    say(f"   {len(seen)} of {len(wanted)} found")


def dump_weapon_attributes(weapon: UObject) -> None:
    say("step 2: GbxAttribute properties of the weapon actor and its components")
    objects = [weapon]
    try:
        objects += list(weapon.K2_GetComponentsByClass(unrealsdk.find_class("ActorComponent")))
    except Exception as ex:  # noqa: BLE001
        say(f"   components: <err {type(ex).__name__}: {ex}>")
    for obj in objects[:40]:
        lines = []
        for prop in obj.Class._properties():
            kind = str(prop.Class.Name)
            if kind != "StructProperty":
                continue
            try:
                inner = str(prop.Struct.Name)
            except Exception:  # noqa: BLE001
                continue
            if inner not in ("GbxAttributeFloat", "GbxAttributeInteger"):
                continue
            try:
                value = getattr(obj, str(prop.Name))
                lines.append(f"{prop.Name}={described(value, 0, 2)}")
            except Exception as ex:  # noqa: BLE001
                lines.append(f"{prop.Name}=<err {type(ex).__name__}>")
        if lines:
            say(f"   {obj.Class.Name} {obj.Name}: " + "; ".join(lines))


def dump_tag_names() -> None:
    say("step 3: criterion tag names")
    for store in unrealsdk.find_all("NexusConfigStoreInventory", exact=False):
        if str(store.Name).startswith("Default__"):
            continue
        try:
            result = store.GetCriterionTagNames()
            tags = [str(name) for name in list(result)] if result is not None else []
            say(f"   {store._path_name()}: {len(tags)} tag(s): {tags}")
        except Exception as ex:  # noqa: BLE001
            say(f"   {store._path_name()}: <err {type(ex).__name__}: {ex}>")
        try:
            result = store.GetCriterionNames()
            say(f"      criterion names: {[str(name) for name in list(result)] if result is not None else []}")
        except Exception as ex:  # noqa: BLE001
            say(f"      criterion names: <err {type(ex).__name__}: {ex}>")


def dump_part_headers(views: DefView, names: NameResolver, identity: int) -> None:
    say("step 4: hidden bytes of the parts and name part tags")
    header = read(identity + 16, 16)
    data, num = u64(header, 0), i32(header, 8)
    raw = read(data, num * 8)
    part_struct = find_struct(PART_STRUCT)
    for index in range(num):
        address = u64(raw, index * 8)
        block = read(address - 24, 24 + 88)
        if block is None:
            continue
        part = views.view(part_struct, address)
        say(f"   [{index}] {described(part.GestaltPartNames)}: header {hex_words(block[:24])} | 0..88 {hex_words(block[24:])}")
        names_in_block = [names.resolve_raw(block[24 + offset:24 + offset + 8]) for offset in range(0, 88, 8)]
        say(f"      words as names: {names_in_block}")


def dump_name_part_tags(views: DefView, names: NameResolver, arena: Arena) -> None:
    struct = find_struct(NAME_PART_STRUCT)
    offsets = {str(p.Name): int(p.Offset_Internal) for p in struct._properties()}
    hits = arena.find_all(int(struct._get_address()).to_bytes(8, "little"), 8, MAX_PART_TRIPLES)
    shown = 0
    seen: set[int] = set()
    for hit in hits:
        raw = read(hit - 8, DEF_PTR_SIZE)
        if raw is None:
            continue
        resolved = u64(raw, 16)
        if resolved in seen or not is_pointer(resolved):
            continue
        seen.add(resolved)
        tags = read(resolved + offsets["RequiredBaseTags"], 32)
        excluded = read(resolved + offsets["ExcludedBaseTags"], 32)
        if tags is None or (tags == bytes(32) and excluded == bytes(32)):
            continue
        view = views.view(struct, resolved)
        say(f"   name part {view.PartName!s} ({names.resolve_raw(raw[:8])}): RequiredBaseTags {hex_words(tags)} | ExcludedBaseTags {hex_words(excluded)}")
        for offset in range(0, 32, 8):
            word = u64(tags, offset)
            if is_pointer(word):
                pointed = read(word, 64)
                if pointed is not None:
                    say(f"      +{offset} -> {hex_words(pointed)} names {[names.resolve_raw(pointed[i:i + 8]) for i in range(0, 64, 8)]}")
        shown += 1
        if shown >= 12:
            break
    say(f"   {shown} name part(s) with tags shown")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v24: attribute defs, weapon attributes, tags")
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
        say(f"   gun: {views.view(find_struct(TYPE_STRUCT), head0).ItemBaseName!s}")
        arena = Arena(head0)
        dump_attribute_defs(views, names, arena)
        dump_weapon_attributes(weapon)
        dump_tag_names()
        dump_part_headers(views, names, identity)
        dump_name_part_tags(views, names, arena)
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
