"""
Phase 0 probe #24: where do the prefixes come from?

probe_names8 found the title: the barrel's naming aspect (inline record type 19076) holds
a TitlePartList with "T.K's Wave" (priority 3). No part of the gun carries a prefix list,
yet the inventory shows "Maintained Screeching T.K's Wave". So the prefixes come from
somewhere else: the type definition's own PrefixPartList, a naming strategy, or a part the
identity keeps in one of its other arrays. This probe works backwards from the words:

1. the definition arena (the 0x7ff4c... region the part definitions live in) is mapped
   with VirtualQuery and read once;
2. every UTF-16 occurrence of each PREFIX is located; from the string buffer the FString
   header, the text data object and the `InventoryNamePartDef` holding it as PartName are
   found by pointer search; then the 24-byte def-pointer entries that point at that
   definition, the TArray headers that hold those entries (the naming aspect's list), and
   the 24-byte inline aspect records that point at that aspect data; finally, for each such
   aspect, whether it sits in one of the gun's twelve part definitions, in the identity's
   head definitions, or elsewhere (address printed);
3. the identity's other arrays are dumped with 8-, 16- and 24-byte element readings, every
   arena pointer in them checked as a part definition, a name part or a naming aspect;
4. the head pointers are read as InventoryTypeDef (ItemBaseName, InventoryName, part lists)
   with the improved text reader.

    pyexec probe_names9.py    with the gun in hand; PREFIXES below match the shotgun

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
PREFIXES = ("Maintained", "Screeching", "T.K's Wave")
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MAX_TEXT_CHARS = 200
MAX_ARENA_BYTES = 768 * 1024 * 1024
MAX_HITS = 8
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
PART_STRUCT = "/Script/GbxGame.InventoryPartDef"
NAMING_STRUCT = "/Script/GbxGame.InventoryNamingAspect"
NAME_PART_STRUCT = "/Script/GbxGame.InventoryNamePartDef"
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
DEF_PTR_SIZE = 24
INLINE_SIZE = 24
IDENTITY_SIZE = 216
ARRAY_SLOTS = ((16, 24), (32, 40), (48, 56), (64, 72), (88, 96))


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
    logging.info(f"[dmgnames9] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def region(address: int) -> MemoryBasicInformation | None:
    info = MemoryBasicInformation()
    if kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
        return None
    return info


def region_readable(info: MemoryBasicInformation) -> bool:
    return (
        info.State == MEM_COMMIT
        and not info.Protect & PAGE_GUARD
        and not info.Protect & PAGE_NOACCESS
        and any(info.Protect & flag for flag in READABLE_PROTECTIONS)
    )


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
        if POINTER_MIN <= word <= POINTER_MAX and word != text_data:
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
    if num <= 0 or num > max_elements or capacity < num or capacity > 4096:
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


def offsets_of(path: str) -> dict[str, int]:
    return {str(prop.Name): int(prop.Offset_Internal) for prop in find_struct(path)._properties()}


class Arena:
    """One snapshot of the definition region around a known definition pointer."""

    def __init__(self, sample_pointer: int) -> None:
        info = region(sample_pointer)
        if info is None:
            raise RuntimeError("no region")
        base = info.AllocationBase or info.BaseAddress or 0
        # Walk forward over the readable regions of the same allocation.
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

    def pointers_to(self, address: int, limit: int = 64) -> list[int]:
        return self.find_all(address.to_bytes(8, "little"), 8, limit)


def chase_prefix(arena: Arena, word: str, offsets: dict[str, dict[str, int]], parts: list[int], heads: list[int]) -> None:
    say(f"   {word!r}:")
    needle = (word + chr(0)).encode("utf-16-le")
    buffers = arena.find_all(needle, 2, MAX_HITS)
    say(f"      {len(buffers)} string buffer(s)")
    name_part_offset = offsets["name_part"]["PartName"]
    seen: set[int] = set()
    for buffer in buffers:
        for header in arena.pointers_to(buffer, 8):
            # header is an FString {data, num, max}, inside a text data object or shared.
            for text_object in arena.pointers_to(header, 8) + [header]:
                for text_field in arena.pointers_to(text_object, 8):
                    name_part = text_field - name_part_offset
                    if name_part in seen or read_ftext(text_field) != word:
                        continue
                    seen.add(name_part)
                    priority_raw = read(name_part + offsets["name_part"]["priority"], 4)
                    say(f"      name part def {name_part:#x} (priority {f32(priority_raw, 0) if priority_raw else '?'})")
                    for pointer_field in arena.pointers_to(name_part, 16):
                        explain_entry(arena, pointer_field - 16, offsets, parts, heads)


def explain_entry(arena: Arena, entry: int, offsets: dict[str, dict[str, int]], parts: list[int], heads: list[int]) -> None:
    """A 24-byte def pointer at `entry` (resolved pointer in its third word) is an element of
    a name part list; find the list's header, the naming aspect data that holds the list,
    the inline aspect record that points at that data, and the definition whose Aspects
    array holds the record."""
    naming_lists = {name: offsets["naming"][name] for name in ("PrefixPartList", "TitlePartList", "SuffixPartList")}
    for stride in range(0, 16):
        array_data = entry - stride * DEF_PTR_SIZE
        for header in arena.pointers_to(array_data, 4):
            for list_name, list_offset in naming_lists.items():
                aspect_data = header - list_offset
                records = arena.pointers_to(aspect_data, 4) + arena.pointers_to(aspect_data + INLINE_SIZE, 4) + arena.pointers_to(aspect_data - INLINE_SIZE, 4)
                if not records:
                    continue
                say(f"         {list_name} header {header:#x} (element {stride} of the list), aspect data {aspect_data:#x}")
                for record_word in records:
                    record_candidates = (record_word, record_word - 8)
                    for record in record_candidates:
                        for record_stride in range(0, 48):
                            aspects_data = record - record_stride * INLINE_SIZE
                            for aspects_header in arena.pointers_to(aspects_data, 4):
                                owner_part = aspects_header - offsets["part"]["Aspects"]
                                owner_type = aspects_header - offsets["type"]["Aspects"]
                                where = f"part def at {owner_part:#x} / type def at {owner_type:#x}"
                                for index, part in enumerate(parts):
                                    if part == owner_part:
                                        where = f"the gun's part [{index}]"
                                for index, head in enumerate(heads):
                                    if head in (owner_part, owner_type):
                                        where = f"the gun's head def {index}"
                                say(f"            record {record:#x} (aspect {record_stride}) in Aspects of {where}")


def dump_other_arrays(identity_address: int, arena: Arena, offsets: dict[str, dict[str, int]]) -> None:
    say("step 3: the identity's other arrays")
    raw = read(identity_address, IDENTITY_SIZE)
    if raw is None:
        return
    for data_offset, count_offset in ARRAY_SLOTS[1:]:
        pointer = u64(raw, data_offset)
        count = i32(raw, count_offset)
        capacity = i32(raw, count_offset + 4)
        say(f"   array at {data_offset}: data {pointer:#x} num {count} max {capacity}")
        if count <= 0 or count > 64:
            continue
        for element_size in (8, 16, 24, 32):
            block = read(pointer, count * element_size)
            if block is None:
                continue
            words = [u64(block, i) for i in range(0, len(block) - 7, 8)]
            say(f"      as {element_size}-byte elements: {[f'{w:#x}' for w in words]}")
        block = read(pointer, count * 8)
        if block is None:
            continue
        for index in range(0, len(block) - 7, 8):
            word = u64(block, index)
            if not (arena.base <= word < arena.base + arena.size):
                continue
            text = read_ftext(word + offsets["name_part"]["PartName"])
            gestalt = read_tarray(word + offsets["part"]["GestaltPartNames"], 8)
            say(f"      word {index // 8} {word:#x}: as name part {text!r}; as part def gestalt count {len(gestalt)}")


def dump_heads(identity_address: int, offsets: dict[str, dict[str, int]]) -> list[int]:
    say("step 4: head pointers as type definitions")
    raw = read(identity_address, 16)
    heads = [u64(raw, 0), u64(raw, 8)] if raw else []
    type_offsets = offsets["type"]
    for head in heads:
        say(f"   head {head:#x}: ItemBaseName {read_ftext(head + type_offsets['ItemBaseName'])!r}, InventoryName {read_ftext(head + type_offsets['InventoryName'])!r}")
        flags = read(head + type_offsets["MaxNumPrefixes"], 8)
        if flags:
            say(f"      MaxNumPrefixes {i32(flags, 0)} MaxNumSuffixes {i32(flags, 4)}")
        for list_name in ("PrefixPartList", "TitlePartList", "SuffixPartList"):
            for entry in read_tarray(head + type_offsets[list_name], DEF_PTR_SIZE, 32):
                target = u64(entry, 16)
                if target >> 40 == 0x7F:
                    priority_raw = read(target + offsets["name_part"]["priority"], 4)
                    say(f"      {list_name}: {read_ftext(target + offsets['name_part']['PartName'])!r} priority {f32(priority_raw, 0) if priority_raw else '?'}")
    return heads


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v9")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
if weapon is None:
    say("no weapon in hand")
else:
    try:
        offsets = {"part": offsets_of(PART_STRUCT), "naming": offsets_of(NAMING_STRUCT), "name_part": offsets_of(NAME_PART_STRUCT), "type": offsets_of(TYPE_STRUCT)}
        identity_address = int(weapon.Item.data.Identity._get_address())
        parts = [u64(element, 0) for element in read_tarray(identity_address + 16, 8)]
        say(f"   {len(parts)} part definitions")
        started = time.time()
        arena = Arena(parts[0])
        say(f"   arena at {arena.base:#x}: {arena.size / 1e6:.1f} MB in {len(arena.chunks)} chunk(s), read in {time.time() - started:.1f} s")
        heads = dump_heads(identity_address, offsets)
        say("step 2: the prefixes, chased backwards")
        for word in PREFIXES:
            try:
                chase_prefix(arena, word, offsets, parts, heads)
            except Exception as ex:  # noqa: BLE001
                say(f"      {word!r} failed: {type(ex).__name__}: {ex}")
        dump_other_arrays(identity_address, arena, offsets)
    except Exception as ex:  # noqa: BLE001
        say(f"   failed: {type(ex).__name__}: {ex}")
say("done")
