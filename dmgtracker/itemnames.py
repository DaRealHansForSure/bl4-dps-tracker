"""
The display names of the equipped weapons, read from the game's own item card model.

The name a gun shows in the inventory ("Maintained Screeching T.K's Wave") is composed by
native code and never stored on the item: the licensed-part prefix comes from a data table
(a Borg magazine on a Jakobs gun gives "Maintained"), the attribute prefix from thresholds
on the gun's attributes ("Screeching" is damage and fire rate over their first thresholds)
and the title from the barrel's naming aspect (poc/probe_names16-20). The finished string
exists in one place the mod can reach: the Coherent UI's item card model of the equipped
weapons, one record per weapon slot in slot order (poc/probe_names25-30, 2026-09-21).

A record (BuildLayout.record_bytes long) holds, as FStrings, the slot type ("weapon"), the name
(with a trailing space), the thumbnail path, the element, the damage text, the item serial
("@Ug...") and the rarity. The records array's header {data, num, max} lies in a 64-byte
node of the UI model pool. The pool is found through two native pointers of the build
(BuildLayout, keyed by the game's changelist): the game instance's UI pointer and, inside
that, the UI model table; the records array is then found by content within that pool's
memory region, so a shifted allocation still resolves, and a changed game build fails the
validation instead of reading nonsense.

Safety: every read goes through ReadProcessMemory, which fails on unmapped pages instead
of faulting; nothing here calls into the game; the discovery scan is bounded; failures
turn the feature off for a while and log once. No SDK imports: the addresses come from the
caller and the tests use fake memory.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from dataclasses import dataclass
from typing import Callable

WEAPON_SLOT_TYPE = "weapon"
SERIAL_PREFIX = "@U"
MAX_RECORDS = 8
MAX_NAME_CHARS = 120
MAX_FIELD_CHARS = 200
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
# Half-widths of the windows around the model table, widened one after the other.
SCAN_WINDOWS_BYTES = (64 * 1024, 1024 * 1024, 8 * 1024 * 1024)
SCAN_CHUNK_BYTES = 1024 * 1024
MEM_COMMIT = 0x1000

MemoryReader = Callable[[int, int], "bytes | None"]
RegionReader = Callable[[int], "tuple[int, int] | None"]


@dataclass(frozen=True)
class BuildLayout:
    """The native offsets of one game build: the UI system pointer inside OakGameInstance,
    the model table array inside that, and the item card record with its FString fields.
    Found with poc/probe_names15, 25-30; keyed by the changelist of
    KismetSystemLibrary.GetBuildVersion() ("Oak2-Win64-4845623-CL-4845623")."""

    changelist: str
    game_instance_ui_offset: int
    ui_model_table_offset: int
    record_bytes: int
    slot_offset: int
    name_offset: int
    element_offset: int
    damage_offset: int
    serial_offset: int
    rarity_offset: int


KNOWN_LAYOUTS: dict[str, BuildLayout] = {
    "4845623": BuildLayout("4845623", 704, 1176, 280, 8, 32, 136, 168, 224, 248),
}
LATEST_LAYOUT = KNOWN_LAYOUTS["4845623"]
CHANGELIST_MARKER = "-CL-"


def layout_for_build(build_version: str) -> tuple[BuildLayout, bool]:
    """The layout for a build version string and whether that build was verified. An
    unknown build gets the latest layout: the validation decides whether it still fits."""
    changelist = build_changelist(build_version)
    layout = KNOWN_LAYOUTS.get(changelist)
    if layout is None:
        return LATEST_LAYOUT, False
    return layout, True


def build_changelist(build_version: str) -> str:
    """'Oak2-Win64-4845623-CL-4845623' -> '4845623'; '' when the string has no changelist."""
    marker = build_version.rfind(CHANGELIST_MARKER)
    if marker < 0:
        return ""
    digits = build_version[marker + len(CHANGELIST_MARKER):].strip()
    return digits if digits.isdigit() else ""


@dataclass(frozen=True)
class ItemCard:
    """One equipped item as the inventory UI describes it."""

    slot_type: str
    name: str
    element: str
    damage_text: str
    serial: str
    rarity: str

    @property
    def is_weapon(self) -> bool:
        return self.slot_type == WEAPON_SLOT_TYPE


class _Kernel:
    """The two Windows calls the reader needs, bound once."""

    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32")
        self.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        self.kernel32.ReadProcessMemory.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
        )
        self.kernel32.ReadProcessMemory.restype = wintypes.BOOL
        self.kernel32.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
        self.kernel32.VirtualQuery.restype = ctypes.c_size_t
        self.process = self.kernel32.GetCurrentProcess()

    def read(self, address: int, size: int) -> bytes | None:
        """The bytes at address, or None when any of them is not readable."""
        if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
            return None
        buffer = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = self.kernel32.ReadProcessMemory(self.process, ctypes.c_void_p(address), buffer, size, ctypes.byref(got))
        if not ok or got.value != size:
            return None
        return buffer.raw

    def region(self, address: int) -> tuple[int, int] | None:
        """(base, size) of the committed memory region holding address, or None."""
        info = _MemoryBasicInformation()
        if self.kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
            return None
        if info.State != MEM_COMMIT:
            return None
        return (info.BaseAddress or 0, int(info.RegionSize))


class _MemoryBasicInformation(ctypes.Structure):
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


def _pointer_at(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def _int_at(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def _is_pointer(word: int) -> bool:
    return POINTER_MIN <= word <= POINTER_MAX


class ItemCardReader:
    """Finds the equipped items' card records and reads them; remembers where they were."""

    def __init__(
        self,
        layout: BuildLayout = LATEST_LAYOUT,
        read_memory: MemoryReader | None = None,
        region_of: RegionReader | None = None,
    ) -> None:
        kernel = _Kernel() if read_memory is None or region_of is None else None
        self.layout = layout
        self._read = read_memory if read_memory is not None else kernel.read
        self._region_of = region_of if region_of is not None else kernel.region
        self._header: int | None = None
        self.last_error = ""

    def cards(self, game_instance_address: int) -> list[ItemCard]:
        """The item cards, in slot order; empty when they cannot be read right now."""
        if self._header is not None:
            cards = self._cards_at(self._header)
            if cards:
                return cards
            self._header = None
        header = self._discover(game_instance_address)
        if header is None:
            return []
        self._header = header
        return self._cards_at(header)

    # ------------------------------------------------------------------ discovery

    def _discover(self, game_instance_address: int) -> int | None:
        """The address of the records array header, found by content in the UI model pool."""
        pool = self._pool_address(game_instance_address)
        if pool is None:
            return None
        region = self._region_of(pool)
        if region is None:
            self.last_error = f"no memory region at {pool:#x}"
            return None
        region_base, region_size = region
        region_end = region_base + region_size
        # The node sits close to the model table in the same pool: widen the window step by
        # step so the usual case costs a few thousand checks, the worst case stays bounded.
        scanned: tuple[int, int] | None = None
        for window in SCAN_WINDOWS_BYTES:
            start = max(region_base, pool - window)
            end = min(region_end, pool + window)
            header = self._scan(start, end, scanned)
            if header is not None:
                return header
            scanned = (start, end)
            if start == region_base and end == region_end:
                break
        self.last_error = f"no item card array near {pool:#x}"
        return None

    def _scan(self, start: int, end: int, skip: tuple[int, int] | None) -> int | None:
        """The first header between start and end, skipping the part scanned before."""
        for chunk_start in range(start, end, SCAN_CHUNK_BYTES):
            chunk = self._read(chunk_start, min(SCAN_CHUNK_BYTES, end - chunk_start))
            if chunk is None:
                continue
            for offset in range(0, len(chunk) - 15, 8):
                address = chunk_start + offset
                if skip is not None and skip[0] <= address < skip[1]:
                    continue
                if self._header_looks_right(chunk, offset):
                    return address
        return None

    def _pool_address(self, game_instance_address: int) -> int | None:
        """The UI model table: a 64-byte allocation in the pool the card records' node uses."""
        ui = self._read(game_instance_address + self.layout.game_instance_ui_offset, 8)
        if ui is None or not _is_pointer(_pointer_at(ui, 0)):
            self.last_error = "no UI pointer in the game instance"
            return None
        table = self._read(_pointer_at(ui, 0) + self.layout.ui_model_table_offset, 8)
        if table is None or not _is_pointer(_pointer_at(table, 0)):
            self.last_error = "no model table in the UI object"
            return None
        return _pointer_at(table, 0)

    def _header_looks_right(self, chunk: bytes, offset: int) -> bool:
        """A TArray header {data, num, max} whose first record is a weapon card."""
        data, num, capacity = _pointer_at(chunk, offset), _int_at(chunk, offset + 8), _int_at(chunk, offset + 12)
        if not _is_pointer(data) or num < 1 or num > MAX_RECORDS or capacity < num or capacity > 2 * MAX_RECORDS:
            return False
        return self._card_at(data) is not None

    # ------------------------------------------------------------------ records

    def _cards_at(self, header: int) -> list[ItemCard]:
        raw = self._read(header, 16)
        if raw is None:
            return []
        data, num, capacity = _pointer_at(raw, 0), _int_at(raw, 8), _int_at(raw, 12)
        if not _is_pointer(data) or num < 1 or num > MAX_RECORDS or capacity < num:
            return []
        cards: list[ItemCard] = []
        for index in range(num):
            card = self._card_at(data + index * self.layout.record_bytes)
            if card is None:
                return []
            cards.append(card)
        return cards

    def _card_at(self, address: int) -> ItemCard | None:
        layout = self.layout
        record = self._read(address, layout.record_bytes)
        if record is None:
            return None
        slot_type = self._fstring(record, layout.slot_offset)
        name = self._fstring(record, layout.name_offset)
        serial = self._fstring(record, layout.serial_offset)
        rarity = self._fstring(record, layout.rarity_offset)
        if not slot_type or not name or not serial or not rarity:
            return None
        if not serial.startswith(SERIAL_PREFIX) or len(name) > MAX_NAME_CHARS:
            return None
        element = self._fstring(record, layout.element_offset) or ""
        damage_text = self._fstring(record, layout.damage_offset) or ""
        return ItemCard(slot_type, name.strip(), element, damage_text, serial, rarity)

    def _fstring(self, record: bytes, offset: int) -> str | None:
        """The text of the FString {data, num, max} at offset; '' for an empty one, None
        when the header is not an FString or the text is not readable."""
        pointer, num, capacity = _pointer_at(record, offset), _int_at(record, offset + 8), _int_at(record, offset + 12)
        if num == 0 and capacity >= 0:
            return ""
        if not _is_pointer(pointer) or num < 1 or num > MAX_FIELD_CHARS or capacity < num:
            return None
        if num == 1:
            return ""
        raw = self._read(pointer, num * 2)
        if raw is None or raw[-2:] != b"\x00\x00":
            return None
        try:
            text = raw[:-2].decode("utf-16-le")
        except UnicodeDecodeError:
            return None
        if any(ord(char) < 32 for char in text):
            return None
        return text


class NameMatcher:
    """Pairs the equipped weapons with the cards, in slot order, and hands a name out only
    once the same pairing was seen twice in a row: the UI model lags an equipment change by
    a moment, and a name read in that moment would belong to the previous gun."""

    def __init__(self) -> None:
        self._last_pairs: dict[str, str] = {}
        self._confirmed: dict[str, str] = {}

    def match(self, item_ids: list[str], cards: list[ItemCard]) -> dict[str, str]:
        """item id -> display name for the pairs confirmed by two consecutive reads."""
        weapon_cards = [card for card in cards if card.is_weapon]
        if not item_ids or len(weapon_cards) != len(item_ids):
            self._last_pairs = {}
            return {}
        pairs = {item_id: card.name for item_id, card in zip(item_ids, weapon_cards) if card.name}
        confirmed = {item_id: name for item_id, name in pairs.items() if self._last_pairs.get(item_id) == name}
        self._last_pairs = pairs
        self._confirmed = confirmed
        return confirmed

    def confirmed(self, item_ids: list[str]) -> bool:
        """True when the last match confirmed a name for every one of these guns."""
        return bool(item_ids) and all(item_id in self._confirmed for item_id in item_ids)
