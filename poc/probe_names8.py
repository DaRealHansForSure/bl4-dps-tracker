"""
Phase 0 probe #23: the name parts behind the gun's part definitions.

probe_names7 proved the identity's part array: twelve `InventoryPartDef` instances with the
struct data at the pointer itself (GestaltPartNames matched the mesh). Each part's Aspects
are inline structs of 24 bytes: two pointers (the second 24 bytes below the first) and two
32-bit values, the first of which looks like the FName index of the aspect's struct type.
This probe:

1. reads the FName index of `InventoryNamingAspect` from the struct object itself (UObject
   NamePrivate at +0x18) and tells the naming aspects apart by it, also trying both pointers
   of every aspect as the aspect data;
2. for each naming aspect reads PrefixPartList / TitlePartList / SuffixPartList (def
   pointers of 24 bytes, the resolved definition being the 0x7f... word) and the
   `InventoryNamePartDef` behind each: PartName (FText, resolved through the text data's
   shared display string), priority and game stage range;
3. prints, per part, everything found, so the composition rule can be checked against the
   name the inventory shows.

    pyexec probe_names8.py    with the gun in hand

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
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MAX_TEXT_CHARS = 200
UOBJECT_NAME_OFFSET = 0x18
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
PART_STRUCT = "/Script/GbxGame.InventoryPartDef"
NAMING_STRUCT = "/Script/GbxGame.InventoryNamingAspect"
NAME_PART_STRUCT = "/Script/GbxGame.InventoryNamePartDef"
LISTS = ("PrefixPartList", "TitlePartList", "SuffixPartList")


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
    logging.info(f"[dmgnames8] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def readable(address: int, size: int) -> bool:
    if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
        return False
    cursor = address
    end = address + size
    while cursor < end:
        info = MemoryBasicInformation()
        if kernel32.VirtualQuery(ctypes.c_void_p(cursor), ctypes.byref(info), ctypes.sizeof(info)) == 0:
            return False
        if info.State != MEM_COMMIT or info.Protect & PAGE_GUARD or info.Protect & PAGE_NOACCESS:
            return False
        if not any(info.Protect & flag for flag in READABLE_PROTECTIONS):
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


def fstring_at(address: int) -> str | None:
    """An FString {TCHAR* data, num, max} at address, when it looks like one."""
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
    """The display string of an ITextData object: an FString inside it, or behind one of
    its pointers (localized text keeps its display string in a shared FString)."""
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


def read_ftext(address: int) -> str:
    header = read(address, 16)
    if header is None:
        return "<text unreadable>"
    text_data = u64(header, 0)
    found = display_string(text_data)
    return found if found is not None else f"<no display string via {text_data:#x}>"


def find_struct(path: str) -> Any:
    try:
        return unrealsdk.find_object("ScriptStruct", path)
    except ValueError:
        wanted = path.rsplit(".", 1)[-1]
        for struct in unrealsdk.find_all("ScriptStruct", exact=True):
            if str(struct.Name) == wanted:
                return struct
        raise


def fields_of(path: str) -> dict[str, Any]:
    return {str(prop.Name): prop for prop in find_struct(path)._properties()}


def fname_index_of(obj: Any) -> int:
    raw = read(int(obj._get_address()) + UOBJECT_NAME_OFFSET, 4)
    return i32(raw, 0) if raw else -1


def name_part_summary(pointer: int, name_part_fields: dict[str, Any]) -> str:
    text = read_ftext(pointer + int(name_part_fields["PartName"].Offset_Internal))
    numbers = read(pointer + int(name_part_fields["priority"].Offset_Internal), 12)
    if numbers is None:
        return f"{pointer:#x} {text!r} (numbers unreadable)"
    return f"{pointer:#x} {text!r} priority {f32(numbers, 0):.2f} stages {i32(numbers, 4)}-{i32(numbers, 8)}"


def naming_lists(base: int, naming_fields: dict[str, Any], name_part_fields: dict[str, Any]) -> list[str]:
    """The three lists read as if `base` were the aspect data; empty when nothing plausible."""
    def_ptr_size = int(naming_fields["TitlePartList"].Inner.ElementSize)
    lines: list[str] = []
    for list_name in LISTS:
        entries = read_tarray(base + int(naming_fields[list_name].Offset_Internal), def_ptr_size, 16)
        for entry in entries:
            words = [u64(entry, offset) for offset in range(0, len(entry) - 7, 8)]
            targets = [word for word in words if word >> 40 == 0x7F and readable(word, 192)]
            for target in targets:
                lines.append(f"{list_name}: {name_part_summary(target, name_part_fields)}")
    return lines


def dump_part(index: int, pointer: int, part_fields: dict[str, Any], naming_fields: dict[str, Any], name_part_fields: dict[str, Any], naming_fname: int) -> None:
    fname_size = int(part_fields["GestaltPartNames"].Inner.ElementSize)
    gestalt = read_tarray(pointer + int(part_fields["GestaltPartNames"].Offset_Internal), fname_size)
    description = fstring_at(pointer + int(part_fields["DebugDisplayDescription"].Offset_Internal))
    say(f"   [{index}] {pointer:#x}: {len(gestalt)} gestalt name(s), description {description!r}")
    inline_size = int(part_fields["Aspects"].Inner.ElementSize)
    aspects = read_tarray(pointer + int(part_fields["Aspects"].Offset_Internal), inline_size, 48)
    for aspect_index, raw in enumerate(aspects):
        words = [u64(raw, 0), u64(raw, 8)]
        type_index, extra = i32(raw, 16), i32(raw, 20)
        # The 32-bit value is not the struct's FName index (108634 for the naming aspect), so
        # every aspect is tried: a naming aspect is one whose lists read as name parts.
        found: list[str] = []
        for base in words:
            if readable(base, 112):
                found = naming_lists(base, naming_fields, name_part_fields)
                if found:
                    break
        marker = "NAMING" if found else ""
        say(f"      aspect {aspect_index}: type fname {type_index} extra {extra} words {words[0]:#x} {words[1]:#x} {marker}")
        for line in found:
            say(f"         {line}")
        if not found and index in (0, 3):
            # Raw look at the data behind both pointers, to place the lists by hand.
            for base in words:
                block = read(base, 128)
                if block is not None:
                    say(f"         raw at {base:#x}: " + " ".join(block[i:i + 8].hex() for i in range(0, 128, 8)))


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v8")
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
        part_fields = fields_of(PART_STRUCT)
        naming_fields = fields_of(NAMING_STRUCT)
        name_part_fields = fields_of(NAME_PART_STRUCT)
        naming_struct = find_struct(NAMING_STRUCT)
        naming_fname = fname_index_of(naming_struct)
        say(f"   InventoryNamingAspect struct {int(naming_struct._get_address()):#x}, FName index {naming_fname}; InventoryPartDef FName index {fname_index_of(find_struct(PART_STRUCT))}")
        identity = weapon.Item.data.Identity
        parts = read_tarray(int(identity._get_address()) + 16, 8)
        say(f"   {len(parts)} part pointer(s); body {weapon.BodyData._path_name()}")
        for index, element in enumerate(parts):
            try:
                dump_part(index, u64(element, 0), part_fields, naming_fields, name_part_fields, naming_fname)
            except Exception as ex:  # noqa: BLE001
                say(f"   [{index}] failed: {type(ex).__name__}: {ex}")
    except Exception as ex:  # noqa: BLE001
        say(f"   failed: {type(ex).__name__}: {ex}")
say("done")
