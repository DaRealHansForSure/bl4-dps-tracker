"""
The display names of the equipped weapons, read from the game's own UI models.

The name a gun shows in the inventory ("Maintained Screeching T.K's Wave") is composed by
native code and never stored on the item: the licensed-part prefix comes from a data table
(a Borg magazine on a Jakobs gun gives "Maintained"), the attribute prefix from thresholds
on the gun's attributes ("Screeching" is damage and fire rate over their first thresholds)
and the title from the barrel's naming aspect (poc/probe_names16-20). The finished string
exists only in the Coherent UI's data models, three of which the mod can read
(poc/probe_names25-30 and poc/probe_run49-57, 2026-09-21):

- the slot records: one record per weapon slot, in slot order, with the type slug
  ("shotgun"), the rarity slug ("legendary") and a copy of the item identity, whose twelve
  seed bytes are the item id the mod keys its records on (dmgtracker/itemidentity.py);
- the item cards: one record per weapon slot, in the same order, with the slot type
  ("weapon"), the name (with a trailing space), the element, the damage text, the item
  serial ("@Ug...") and the rarity, but no seed;
- the inventory list: one record per item the inventory screen last showed, with the
  seed, the name, the serial and the slugs together. It is a snapshot: a gun equipped from
  the ground is missing until the screen is opened again.

Nothing points at these arrays from any object the SDK can name (the holders are the UI's
binding caches), and their heap position changes from session to session, so they are
found by content: a scan of the process memory for the seed of an equipped gun hits its
slot record and, when present, its inventory record. The item cards lie in the same heap
block as the slot records (adjacent in one session, 9 KB away in another), so they are
found by content in a window around the slot records and paired with them by index. A
pairing counts once two consecutive reads agree, because the UI models lag an equipment
change by a moment; a name from the inventory list needs no confirmation, its record
carries the seed. The record layouts are keyed by the game build (BuildLayout).

Safety: every read goes through ReadProcessMemory, which fails on unmapped pages instead
of faulting, and only after VirtualQuery said the pages are committed and not guard pages
(touching a guard page disarms it, which crashes the thread that owns it later); nothing
here calls into the game; the scan runs on its own thread in small chunks with pauses (8 s
for 7 GB), the game thread only ever reads remembered addresses, and a scan that found
nothing rests longer each time. No SDK imports: the seeds come from the caller and the
tests use fake memory.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

WEAPON_SLOT_TYPE = "weapon"
SERIAL_PREFIX = "@U"
SEED_BYTES = 12
MAX_SLOT_RECORDS = 8
MAX_CARD_RECORDS = 8
MAX_LIST_RECORDS = 400
MAX_NAME_CHARS = 120
MAX_SLUG_CHARS = 40
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
RECORD_ALIGNMENT = 8
PAGE_BYTES = 4096
# The scan reads a megabyte at a time, overlapping the chunks so a seed on a chunk boundary
# is still seen, and rests a moment every few megabytes to leave the game thread alone.
SCAN_CHUNK_BYTES = 1024 * 1024
SCAN_OVERLAP_BYTES = 64
SCAN_REST_EVERY_BYTES = 8 * 1024 * 1024
SCAN_REST_S = 0.001
# Half-width of the window around the slot records in which the item cards are looked for,
# read in pieces that overlap by one record so a card on a piece boundary is still seen.
CARDS_WINDOW_BYTES = 256 * 1024
WINDOW_PIECE_BYTES = 64 * 1024
# The remembered inventory list is read again this often at most while a name is missing.
LIST_REREAD_INTERVAL_S = 5.0
# A scan that found nothing rests this long before the next one, twice as long each
# time: the first scan of a session often runs before the UI has built its models.
SCAN_RETRY_FIRST_S = 15.0
SCAN_RETRY_MAX_S = 600.0
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_NOACCESS = 0x01
PAGE_READWRITE = 0x04
PAGE_GUARD = 0x100

# (address, size) -> the readable bytes there: all, a shorter prefix, or None.
MemoryReader = Callable[[int, int], "bytes | None"]
# () -> the (base, size) of every committed, private, writable region.
RegionLister = Callable[[], Iterable[tuple[int, int]]]


@dataclass(frozen=True)
class SlotRecordLayout:
    """One weapon slot as the UI models it: two slug FStrings and a copy of the identity."""

    record_bytes: int
    type_offset: int
    rarity_offset: int
    seed_offset: int


@dataclass(frozen=True)
class CardRecordLayout:
    """One item card: FStrings for the slot type, the name, the element, the damage text,
    the serial and the rarity."""

    record_bytes: int
    slot_offset: int
    name_offset: int
    element_offset: int
    damage_offset: int
    serial_offset: int
    rarity_offset: int


@dataclass(frozen=True)
class ListRecordLayout:
    """One inventory list entry: slug and text FStrings, the serial, and a copy of the
    identity with the seed."""

    record_bytes: int
    slot_offset: int
    type_offset: int
    name_offset: int
    rarity_offset: int
    element_offset: int
    serial_offset: int
    seed_offset: int


@dataclass(frozen=True)
class BuildLayout:
    """The record layouts of one game build, keyed by the changelist of
    KismetSystemLibrary.GetBuildVersion() ("Oak2-Win64-4845623-CL-4845623")."""

    changelist: str
    slots: SlotRecordLayout
    cards: CardRecordLayout
    inventory: ListRecordLayout


KNOWN_LAYOUTS: dict[str, BuildLayout] = {
    "4845623": BuildLayout(
        "4845623",
        SlotRecordLayout(288, 0, 24, 248),
        CardRecordLayout(280, 8, 32, 136, 168, 224, 248),
        ListRecordLayout(1072, 0, 24, 80, 184, 296, 392, 632),
    ),
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
    """One item as the inventory UI describes it."""

    slot_type: str
    name: str
    element: str
    damage_text: str
    serial: str
    rarity: str

    @property
    def is_weapon(self) -> bool:
        return self.slot_type == WEAPON_SLOT_TYPE


@dataclass(frozen=True)
class SlotRecord:
    """One weapon slot: what kind of gun sits in it and which one."""

    type_slug: str
    rarity: str
    seed: bytes


@dataclass(frozen=True)
class EquippedModels:
    """Where the slot records and the item cards were found."""

    slots_address: int
    cards_address: int


class _Kernel:
    """ReadProcessMemory and VirtualQuery on the own process, bound once."""

    class _RegionInfo(ctypes.Structure):
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
        # A scratch buffer per thread (the scan thread and the game thread read at the
        # same time): a fresh megabyte per chunk would cost more than the read itself.
        self._scratch = threading.local()

    def read(self, address: int, size: int) -> bytes | None:
        """The readable bytes at address: all of them, a shorter prefix when the range runs
        into a page that must not be read, None when not even the first byte is readable."""
        if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
            return None
        size = self._readable_length(address, size)
        if size <= 0:
            return None
        buffer = self._buffer_for(size)
        got = ctypes.c_size_t(0)
        self.kernel32.ReadProcessMemory(self.process, ctypes.c_void_p(address), buffer, size, ctypes.byref(got))
        if got.value == 0:
            return None
        return ctypes.string_at(buffer, got.value)

    def _readable_length(self, address: int, size: int) -> int:
        """How many of the bytes at address lie in committed pages that may be read. A
        read must never touch a guard page: ReadProcessMemory clears the guard on the way
        through, and a thread whose stack lost its guard page crashes later."""
        end = address + size
        position = address
        while position < end:
            info = self._RegionInfo()
            if self.kernel32.VirtualQuery(ctypes.c_void_p(position), ctypes.byref(info), ctypes.sizeof(info)) == 0:
                break
            if info.State != MEM_COMMIT or info.Protect & (PAGE_GUARD | PAGE_NOACCESS):
                break
            position = (info.BaseAddress or 0) + info.RegionSize
        return min(position, end) - address

    def _buffer_for(self, size: int) -> ctypes.Array:
        buffer = getattr(self._scratch, "buffer", None)
        if buffer is None or len(buffer) < size:
            buffer = ctypes.create_string_buffer(max(size, SCAN_CHUNK_BYTES + SCAN_OVERLAP_BYTES))
            self._scratch.buffer = buffer
        return buffer

    def regions(self) -> Iterable[tuple[int, int]]:
        """Every committed, private, read-write region: the heaps, where the UI keeps its
        models. Image, mapped and guarded memory is left out."""
        address = 0
        while address < POINTER_MAX:
            info = self._RegionInfo()
            if self.kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
                return
            base = info.BaseAddress or 0
            size = info.RegionSize
            if info.State == MEM_COMMIT and info.Type == MEM_PRIVATE and info.Protect == PAGE_READWRITE:
                yield base, size
            address = base + size


def _pointer_at(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def _int_at(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def _is_pointer(word: int) -> bool:
    return POINTER_MIN <= word <= POINTER_MAX


def _align_up(value: int, alignment: int) -> int:
    return -(-value // alignment) * alignment


def _is_slug(text: str | None) -> bool:
    """A lower-case identifier like 'shotgun', 'legendary' or 'classmod_dark_siren'."""
    if not text or len(text) > MAX_SLUG_CHARS:
        return False
    return all(char.isalnum() or char == "_" for char in text)


class ItemNameReader:
    """Answers seed -> item name for the equipped weapons from the UI models; finds the
    models by content on a thread of its own and remembers where they were."""

    def __init__(
        self,
        layout: BuildLayout = LATEST_LAYOUT,
        read_memory: MemoryReader | None = None,
        list_regions: RegionLister | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        kernel = _Kernel() if read_memory is None or list_regions is None else None
        self.layout = layout
        self._read = read_memory if read_memory is not None else kernel.read
        self._regions = list_regions if list_regions is not None else kernel.regions
        self._clock = clock
        self._lock = threading.Lock()
        self._names: dict[bytes, str] = {}
        self._last_pairs: dict[bytes, str] = {}
        self._models: EquippedModels | None = None
        self._list_address: int | None = None
        self._last_list_read = 0.0
        self._scan: threading.Thread | None = None
        self._next_scan_at = 0.0
        self._retry_s = SCAN_RETRY_FIRST_S
        self.scans = 0
        self.last_error = ""

    # ------------------------------------------------------------------ game thread

    def names_for(self, seeds: Iterable[bytes]) -> dict[bytes, str]:
        """seed -> item name for the seeds whose name is known. A missing name makes the
        remembered models be read again (a few small reads) and, when they do not have it
        either, starts a scan on its own thread; the name then arrives on a later call."""
        wanted = [seed for seed in seeds if len(seed) == SEED_BYTES]
        missing = [seed for seed in wanted if seed not in self._names]
        if missing:
            self._refresh_remembered(missing)
        # A seed the remembered models pair already is waiting for its confirming read,
        # not for a scan.
        unknown = [seed for seed in wanted if seed not in self._names and seed not in self._last_pairs]
        if unknown:
            self._start_scan(unknown[0])
        return {seed: self._names[seed] for seed in wanted if seed in self._names}

    def forget(self) -> None:
        """Drop every name and every remembered address; the next read starts over."""
        with self._lock:
            self._names = {}
            self._last_pairs = {}
            self._models = None
            self._list_address = None
            self._next_scan_at = 0.0
            self._retry_s = SCAN_RETRY_FIRST_S

    @property
    def discovering(self) -> bool:
        return self._scan is not None and self._scan.is_alive()

    @property
    def known(self) -> int:
        return len(self._names)

    def _refresh_remembered(self, missing: list[bytes]) -> None:
        models = self._models
        if models is not None and not self._learn_from_models(models):
            self._models = None
            self._last_pairs = {}
        list_address = self._list_address
        list_due = self._clock() - self._last_list_read >= LIST_REREAD_INTERVAL_S
        if list_address is not None and list_due and any(seed not in self._names for seed in missing):
            self._last_list_read = self._clock()
            if not self._learn_from_list(list_address):
                self._list_address = None

    def _start_scan(self, seed: bytes) -> None:
        if self.discovering or self._clock() < self._next_scan_at:
            return
        self._scan = threading.Thread(target=self._scan_in_background, args=(seed,), name="dmgtracker-item-names", daemon=True)
        self._scan.start()

    # ------------------------------------------------------------------ learning

    def _learn_from_models(self, models: EquippedModels) -> bool:
        """Pair the slot records with the item cards at the remembered addresses; a pair
        becomes a name once the previous read had the same pair. False when the models
        are gone from there."""
        slots = self._slots_at(models.slots_address)
        cards = self._cards_at(models.cards_address)
        if not slots or not cards:
            return False
        pairs = self._pair(slots, cards)
        with self._lock:
            for seed, name in pairs.items():
                if self._last_pairs.get(seed) == name:
                    self._names[seed] = name
            self._last_pairs = pairs
        return True

    def _learn_from_list(self, list_address: int) -> bool:
        """Take every seed -> name of the inventory list at the remembered address; False
        when there is no list there any more."""
        entries = self._list_at(list_address)
        if not entries:
            return False
        with self._lock:
            for seed, card in entries.items():
                self._names[seed] = card.name
        return True

    def _pair(self, slots: list[SlotRecord], cards: list[ItemCard]) -> dict[bytes, str]:
        """Slot i holds the gun card i describes. The rarity is in both, so one pair whose
        rarities differ means a stale or shifted model, and nothing is paired."""
        pairs: dict[bytes, str] = {}
        for slot, card in zip(slots, cards):
            if not card.is_weapon or card.rarity != slot.rarity or not card.name:
                return {}
            pairs[slot.seed] = card.name
        return pairs

    # ------------------------------------------------------------------ scan thread

    def _scan_in_background(self, seed: bytes) -> None:
        self.scans += 1
        try:
            found = self._scan_for(seed)
        except Exception as ex:  # noqa: BLE001 - a scan that fails is a scan that found nothing
            self.last_error = f"{type(ex).__name__}: {ex}"
            found = False
        if found:
            self._retry_s = SCAN_RETRY_FIRST_S
            return
        self._next_scan_at = self._clock() + self._retry_s
        self._retry_s = min(self._retry_s * 2, SCAN_RETRY_MAX_S)

    def _scan_for(self, seed: bytes) -> bool:
        """Walk the heaps for the seed; every hit is tried as a slot record (with the item
        cards near it) and as an inventory record. True once the seed has a name or its
        slot record and cards were found (the name follows with the confirming read)."""
        since_rest = 0
        for base, size in self._regions():
            for chunk_start in range(base, base + size, SCAN_CHUNK_BYTES):
                chunk = self._read(chunk_start, min(SCAN_CHUNK_BYTES + SCAN_OVERLAP_BYTES, base + size - chunk_start))
                if chunk is None:
                    continue
                position = chunk.find(seed)
                while 0 <= position < SCAN_CHUNK_BYTES:
                    if self._learn_around(chunk_start + position, seed):
                        return True
                    position = chunk.find(seed, position + 1)
                since_rest += len(chunk)
                if since_rest >= SCAN_REST_EVERY_BYTES:
                    since_rest = 0
                    time.sleep(SCAN_REST_S)
        self.last_error = f"no UI model holds the seed {seed.hex()[:6]}"
        return False

    def _learn_around(self, hit: int, seed: bytes) -> bool:
        """One hit of the seed: an inventory record gives the name at once, a slot record
        gives the models to confirm on the next read."""
        list_record = hit - self.layout.inventory.seed_offset
        if self._list_record_at(list_record) is not None:
            list_address = self._list_start(list_record)
            self._list_address = list_address
            self._last_list_read = self._clock()
            self._learn_from_list(list_address)
            if seed in self._names:
                return True
        slot_record = hit - self.layout.slots.seed_offset
        if self._slot_at(slot_record) is None:
            return False
        slots_address = self._slots_start(slot_record)
        cards_address = self._cards_near(slots_address, self._slots_at(slots_address))
        if cards_address is None:
            return False
        models = EquippedModels(slots_address, cards_address)
        self._models = models
        return self._learn_from_models(models) and seed in self._last_pairs

    def _cards_near(self, slots_address: int, slots: list[SlotRecord]) -> int | None:
        """The start of the item card array in the window around the slot records, found
        by content. Of several arrays there (a freed copy can linger) the one that pairs
        with the slot records wins, the nearest of those."""
        candidates: list[tuple[int, int]] = []
        for address, cards in self._card_arrays_in(slots_address - CARDS_WINDOW_BYTES, slots_address + CARDS_WINDOW_BYTES):
            if self._pair(slots, cards):
                candidates.append((abs(address - slots_address), address))
        if not candidates:
            return None
        return min(candidates)[1]

    def _card_arrays_in(self, start: int, end: int) -> Iterable[tuple[int, list[ItemCard]]]:
        """Every run of item card records in the window, each with its start address."""
        record_bytes = self.layout.cards.record_bytes
        seen_up_to = 0
        for piece_start, piece in self._readable_pieces(max(POINTER_MIN, start), end, record_bytes):
            for offset in range(0, len(piece) - record_bytes, RECORD_ALIGNMENT):
                address = piece_start + offset
                if address < seen_up_to or not self._card_prefilter(piece, offset) or self._card_at(address) is None:
                    continue
                while self._card_at(address - record_bytes) is not None:
                    address -= record_bytes
                cards = self._cards_at(address)
                seen_up_to = address + len(cards) * record_bytes
                yield address, cards

    def _card_prefilter(self, data: bytes, offset: int) -> bool:
        """Cheap checks on the slot type and name FString headers before any read."""
        layout = self.layout.cards
        for field_offset, max_chars in ((layout.slot_offset, MAX_SLUG_CHARS), (layout.name_offset, MAX_NAME_CHARS)):
            start = offset + field_offset
            num, capacity = _int_at(data, start + 8), _int_at(data, start + 12)
            if not _is_pointer(_pointer_at(data, start)) or num < 2 or num > max_chars + 1 or capacity < num:
                return False
        return True

    def _readable_pieces(self, start: int, end: int, overlap: int) -> Iterable[tuple[int, bytes]]:
        """The window in page-aligned pieces that overlap by the given bytes; an unreadable
        page is skipped, a read that stops short continues behind the page that stopped it."""
        position = start - start % PAGE_BYTES
        while position < end:
            piece = self._read(position, min(WINDOW_PIECE_BYTES + overlap, end - position))
            if piece is None:
                position += PAGE_BYTES
                continue
            yield position, piece
            if len(piece) < WINDOW_PIECE_BYTES + overlap and position + len(piece) < end:
                position = _align_up(position + len(piece), PAGE_BYTES)
            else:
                position += WINDOW_PIECE_BYTES

    # ------------------------------------------------------------------ records

    def _slots_start(self, record: int) -> int:
        stride = self.layout.slots.record_bytes
        steps = 0
        while steps < MAX_SLOT_RECORDS and self._slot_at(record - stride) is not None:
            record -= stride
            steps += 1
        return record

    def _list_start(self, record: int) -> int:
        stride = self.layout.inventory.record_bytes
        steps = 0
        while steps < MAX_LIST_RECORDS and self._list_record_at(record - stride) is not None:
            record -= stride
            steps += 1
        return record

    def _slots_at(self, address: int) -> list[SlotRecord]:
        slots: list[SlotRecord] = []
        while len(slots) < MAX_SLOT_RECORDS:
            slot = self._slot_at(address + len(slots) * self.layout.slots.record_bytes)
            if slot is None:
                break
            slots.append(slot)
        return slots

    def _cards_at(self, address: int) -> list[ItemCard]:
        cards: list[ItemCard] = []
        while len(cards) < MAX_CARD_RECORDS:
            card = self._card_at(address + len(cards) * self.layout.cards.record_bytes)
            if card is None:
                break
            cards.append(card)
        return cards

    def _list_at(self, address: int) -> dict[bytes, ItemCard]:
        entries: dict[bytes, ItemCard] = {}
        while len(entries) < MAX_LIST_RECORDS:
            entry = self._list_record_at(address + len(entries) * self.layout.inventory.record_bytes)
            if entry is None:
                break
            seed, card = entry
            entries[seed] = card
        return entries

    def _slot_at(self, address: int) -> SlotRecord | None:
        layout = self.layout.slots
        record = self._read_exact(address, layout.record_bytes)
        if record is None:
            return None
        type_slug = self._fstring(record, layout.type_offset)
        rarity = self._fstring(record, layout.rarity_offset)
        seed = record[layout.seed_offset:layout.seed_offset + SEED_BYTES]
        if not _is_slug(type_slug) or not _is_slug(rarity) or not any(seed):
            return None
        return SlotRecord(type_slug, rarity, seed)

    def _card_at(self, address: int) -> ItemCard | None:
        layout = self.layout.cards
        record = self._read_exact(address, layout.record_bytes)
        if record is None:
            return None
        slot_type = self._fstring(record, layout.slot_offset)
        name = self._fstring(record, layout.name_offset)
        serial = self._fstring(record, layout.serial_offset)
        rarity = self._fstring(record, layout.rarity_offset)
        if not _is_slug(slot_type) or not name or not _is_slug(rarity) or not serial:
            return None
        if not serial.startswith(SERIAL_PREFIX) or len(name) > MAX_NAME_CHARS:
            return None
        element = self._fstring(record, layout.element_offset) or ""
        damage_text = self._fstring(record, layout.damage_offset) or ""
        return ItemCard(slot_type, name.strip(), element, damage_text, serial, rarity)

    def _list_record_at(self, address: int) -> tuple[bytes, ItemCard] | None:
        layout = self.layout.inventory
        record = self._read_exact(address, layout.record_bytes)
        if record is None:
            return None
        slot_type = self._fstring(record, layout.slot_offset)
        name = self._fstring(record, layout.name_offset)
        serial = self._fstring(record, layout.serial_offset)
        rarity = self._fstring(record, layout.rarity_offset)
        seed = record[layout.seed_offset:layout.seed_offset + SEED_BYTES]
        if not _is_slug(slot_type) or not name or not _is_slug(rarity) or not serial or not any(seed):
            return None
        if not serial.startswith(SERIAL_PREFIX) or len(name) > MAX_NAME_CHARS:
            return None
        element = self._fstring(record, layout.element_offset) or ""
        return seed, ItemCard(slot_type, name.strip(), element, "", serial, rarity)

    def _read_exact(self, address: int, size: int) -> bytes | None:
        """The bytes at address, or None unless all of them are readable."""
        data = self._read(address, size)
        if data is None or len(data) < size:
            return None
        return data

    def _fstring(self, record: bytes, offset: int) -> str | None:
        """The text of the FString {data, num, max} at offset; '' for an empty one, None
        when the header is not an FString or the text is not readable."""
        pointer, num, capacity = _pointer_at(record, offset), _int_at(record, offset + 8), _int_at(record, offset + 12)
        if num == 0 and capacity >= 0:
            return ""
        if not _is_pointer(pointer) or num < 1 or num > MAX_NAME_CHARS + 1 or capacity < num:
            return None
        if num == 1:
            return ""
        raw = self._read_exact(pointer, num * 2)
        if raw is None or raw[-2:] != b"\x00\x00":
            return None
        try:
            text = raw[:-2].decode("utf-16-le")
        except UnicodeDecodeError:
            return None
        if any(ord(char) < 32 for char in text):
            return None
        return text
