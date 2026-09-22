"""
The display names of the equipped weapons, read from the game's own UI models.

The name a gun shows in the inventory ("Maintained Screeching T.K's Wave") is composed by
native code and never stored on the item: the licensed-part prefix comes from a data table
(a Borg magazine on a Jakobs gun gives "Maintained"), the attribute prefix from thresholds
on the gun's attributes ("Screeching" is damage and fire rate over their first thresholds)
and the title from the barrel's naming aspect (poc/probe_names16-20). The finished string
exists only in the Coherent UI's data models, two of which the mod reads
(poc/probe_names25-30 and poc/probe_run49-75, 2026-09-21):

- the item cards: one record per equipped weapon, in slot order, with the slot type
  ("weapon"), the name (with a trailing space), the thumbnail path, the element, the
  damage text, the item serial ("@Ug...") and the rarity. They exist from the first second
  in the world, no inventory screen needed;
- the inventory list: one record per item the inventory screen last showed, with the
  seed of the item identity (the item id the mod keys its records on,
  dmgtracker/itemidentity.py), the name, the serial and the slugs together. A snapshot: it
  appears once the inventory screen was opened and misses a gun equipped from the ground.

Nothing points at these arrays from any object the SDK can name, and their heap position
changes from session to session (the card records were 9 KB from their neighbours in one
session and hundreds of MB away in the next), so both are found by content in one pass
over the process memory: the inventory record by the seed of an equipped gun, and the item
cards by the node that holds their array header, which has the same shape in every
session seen: a one-element array header right before {data, num, max}, two ints, a zero
and a minus one right after. The cards are paired with the weapons by slot order (the
caller's list of seeds, the game's own slot array), and a pairing counts once two
consecutive reads agree, because the UI model lags an equipment change by a moment; a
name from the inventory list needs no confirmation, its record carries the seed. The
record layouts and the node shape are keyed by the game build (BuildLayout).

Safety: every read goes through ReadProcessMemory, which fails on unmapped pages instead
of faulting, and only after VirtualQuery said the pages are committed and not guard pages
(touching a guard page disarms it, which crashes the thread that owns it later); nothing
here calls into the game; the scan runs on its own thread in small chunks with pauses
(seconds, the regions the UI's allocator favours first), the game thread only ever reads
remembered addresses, and a scan that found nothing rests longer each time. No SDK
imports: the seeds come from the caller and the tests use fake memory.
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
MAX_CARD_RECORDS = 8
MAX_LIST_RECORDS = 400
MAX_NAME_CHARS = 120
MAX_SLUG_CHARS = 40
MAX_PATH_CHARS = 200
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
RECORD_ALIGNMENT = 8
# The scan reads a megabyte at a time, overlapping the chunks so a needle on a chunk
# boundary is still seen, and rests a moment every few megabytes to leave the game thread
# alone.
SCAN_CHUNK_BYTES = 1024 * 1024
SCAN_OVERLAP_BYTES = 128
SCAN_REST_EVERY_BYTES = 8 * 1024 * 1024
SCAN_REST_S = 0.001
# The remembered inventory list is read again this often at most while a name is missing.
LIST_REREAD_INTERVAL_S = 5.0
# A scan that found nothing rests this long before the next one, twice as long each
# time: the first scan of a session often runs before the UI has built its models.
SCAN_RETRY_FIRST_S = 15.0
SCAN_RETRY_MAX_S = 600.0
# The scan reads the regions around the last model it found first (a 1 GB window), or,
# knowing none, the address band the UI's allocator used in every session seen so far
# (0x31.. to 0x34..); the rest of the heaps follows, so the order only changes the time.
SCAN_HINT_RADIUS = 512 * 1024 * 1024
DEFAULT_SCAN_BAND = (0x300000000, 0x380000000)
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
class CardRecordLayout:
    """One item card: FStrings for the slot type, the name, the element, the damage text,
    the serial and the rarity."""

    record_bytes: int
    slot_offset: int
    name_offset: int
    thumbnail_offset: int
    element_offset: int
    damage_offset: int
    serial_offset: int
    rarity_offset: int


@dataclass(frozen=True)
class CardsNodeLayout:
    """The node that holds the item cards' array header {data, num, max}, as bytes around
    the header: a one-element array header ({1, 1}) right before it and, after the two
    ints that follow the header, an 8-byte zero and an 8-byte minus one. The array's
    capacity may exceed its count by a few records."""

    before: bytes
    after_offset: int
    after: bytes
    spare_records: int

    @property
    def window_bytes(self) -> int:
        """The bytes from the start of the preceding header to the end of the tail."""
        return len(self.before) + self.after_offset + len(self.after)


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
    thumbnail_offset: int
    serial_offset: int
    seed_offset: int


@dataclass(frozen=True)
class BuildLayout:
    """The record layouts of one game build, keyed by the changelist of
    KismetSystemLibrary.GetBuildVersion() ("Oak2-Win64-4845623-CL-4845623")."""

    changelist: str
    cards: CardRecordLayout
    cards_node: CardsNodeLayout
    inventory: ListRecordLayout


KNOWN_LAYOUTS: dict[str, BuildLayout] = {
    "4845623": BuildLayout(
        "4845623",
        CardRecordLayout(280, 8, 32, 56, 136, 168, 224, 248),
        CardsNodeLayout(b"\x01\x00\x00\x00\x01\x00\x00\x00", 32, bytes(8) + b"\xff" * 8, 4),
        ListRecordLayout(1072, 0, 24, 80, 184, 296, 368, 392, 632),
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
    """One item as the inventory UI describes it. The thumbnail is the export text of the
    texture the card shows: a render target the game drew the gun into
    (`TextureRenderTarget2D'/Engine/Transient...:OakInventoryThumbnail_...'`) or a static
    type icon (`Texture2D'/Game/uiresources/...'`)."""

    slot_type: str
    name: str
    element: str
    damage_text: str
    serial: str
    rarity: str
    thumbnail: str = ""

    @property
    def is_weapon(self) -> bool:
        return self.slot_type == WEAPON_SLOT_TYPE


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


def _is_slug(text: str | None) -> bool:
    """A lower-case identifier like 'shotgun', 'legendary' or 'classmod_dark_siren'."""
    if not text or len(text) > MAX_SLUG_CHARS:
        return False
    return all(char.isalnum() or char == "_" for char in text)


def _merge_cards(known: ItemCard | None, fresh: ItemCard) -> ItemCard:
    """The fresh card with its blank fields filled from the known one."""
    if known is None:
        return fresh
    return ItemCard(
        fresh.slot_type or known.slot_type,
        fresh.name or known.name,
        fresh.element or known.element,
        fresh.damage_text or known.damage_text,
        fresh.serial or known.serial,
        fresh.rarity or known.rarity,
        fresh.thumbnail or known.thumbnail,
    )


def _search_needle(pattern: bytes) -> bytes:
    """The pattern without its trailing zero bytes: bytes.find is slowest on a needle that
    ends in a byte as common as zero, so the zeros are checked after a match instead."""
    stripped = pattern.rstrip(b"\x00")
    return stripped if stripped else pattern


class ItemNameReader:
    """Answers seed -> item name for the equipped weapons from the UI models; finds the
    models by content on a thread of its own and remembers where they were."""

    def __init__(
        self,
        layout: BuildLayout = LATEST_LAYOUT,
        read_memory: MemoryReader | None = None,
        list_regions: RegionLister | None = None,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ) -> None:
        kernel = _Kernel() if read_memory is None or list_regions is None else None
        self.layout = layout
        self._read = read_memory if read_memory is not None else kernel.read
        self._regions = list_regions if list_regions is not None else kernel.regions
        self._clock = clock
        self._log = log if log is not None else (lambda message: None)
        self._lock = threading.Lock()
        self._names: dict[bytes, str] = {}
        self._cards: dict[bytes, ItemCard] = {}
        self._last_pairs: dict[bytes, str] = {}
        # The same for a session without seeds: the confirmed card of each weapon slot, and
        # the cards the previous read saw there.
        self._slot_cards: dict[int, ItemCard] = {}
        self._last_slot_cards: dict[int, ItemCard] = {}
        self._cards_header: int | None = None
        self._list_address: int | None = None
        self._last_list_read = 0.0
        self._scan: threading.Thread | None = None
        self._scan_hint: int | None = None
        self._next_scan_at = 0.0
        self._retry_s = SCAN_RETRY_FIRST_S
        self.scans = 0
        self.last_scan_s = 0.0
        self.last_error = ""

    # ------------------------------------------------------------------ game thread

    def names_for(self, slot_seeds: list[bytes | None]) -> dict[bytes, str]:
        """seed -> item name for the equipped weapons, given their seeds in slot order
        (None for a gun without one). A missing name makes the remembered models be read
        again (a few small reads) and, when they do not have it either, starts a scan on
        its own thread; the name then arrives on a later call."""
        wanted = [seed for seed in slot_seeds if seed is not None and len(seed) == SEED_BYTES]
        missing = [seed for seed in wanted if seed not in self._names]
        if missing:
            self._refresh_remembered(slot_seeds, missing)
        # A seed the remembered cards pair already is waiting for its confirming read,
        # not for a scan.
        unknown = [seed for seed in wanted if seed not in self._names and seed not in self._last_pairs]
        if unknown:
            self._start_scan(unknown[0], list(slot_seeds))
        return {seed: self._names[seed] for seed in wanted if seed in self._names}

    def cards_by_slot(self, slot_count: int) -> dict[int, ItemCard]:
        """The card of each weapon slot, by slot index, for a session where the guns have no
        seed of their own (a co-op client: the identity is not replicated). The cards model
        describes the equipped guns in slot order, so the slot is the only key needed. Two
        reads must agree, as for the seed pairing, because the model lags an equipment
        change by a moment."""
        header = self._cards_header
        if header is not None and not self._learn_slot_cards(header, slot_count):
            self._cards_header = None
        if self._cards_header is None:
            self._start_scan(b"", [], want_cards=True)
        with self._lock:
            return dict(self._slot_cards)

    def card_for(self, seed: bytes) -> ItemCard | None:
        """The card of an item whose name is known: what the inventory says about it."""
        return self._cards.get(seed)

    def names_of_items(self, seeds: list[bytes]) -> dict[bytes, str]:
        """seed -> name for items that are not weapons (the grenade, say), which only the
        inventory list names. A missing one makes the remembered list be read again or,
        without a list, starts a scan for the list alone (the cards do not name gadgets)."""
        found = {seed: self._names[seed] for seed in seeds if seed in self._names}
        missing = [seed for seed in seeds if seed not in found]
        if not missing:
            return found
        list_address = self._list_address
        if list_address is not None and self._clock() - self._last_list_read >= LIST_REREAD_INTERVAL_S:
            self._last_list_read = self._clock()
            if self._learn_from_list(list_address):
                found.update({seed: self._names[seed] for seed in missing if seed in self._names})
            else:
                self._list_address = None
        if self._list_address is None:
            self._start_scan(missing[0], [], want_cards=False)
        return found

    def retry_now(self) -> None:
        """Cut a failed scan's rest short: the UI changed (a menu opened or closed), so a
        model that was not there may be there now."""
        self._next_scan_at = 0.0
        self._retry_s = SCAN_RETRY_FIRST_S

    def forget(self) -> None:
        """Drop every name and every remembered address; the next read starts over."""
        with self._lock:
            self._names = {}
            self._cards = {}
            self._last_pairs = {}
            self._cards_header = None
            self._slot_cards = {}
            self._last_slot_cards = {}
            self._list_address = None
            self._next_scan_at = 0.0
            self._retry_s = SCAN_RETRY_FIRST_S

    @property
    def discovering(self) -> bool:
        return self._scan is not None and self._scan.is_alive()

    @property
    def known(self) -> int:
        return len(self._names)

    def _refresh_remembered(self, slot_seeds: list[bytes | None], missing: list[bytes]) -> None:
        header = self._cards_header
        if header is not None and not self._learn_from_cards(header, slot_seeds):
            self._cards_header = None
            self._last_pairs = {}
        list_address = self._list_address
        list_due = self._clock() - self._last_list_read >= LIST_REREAD_INTERVAL_S
        if list_address is not None and list_due and any(seed not in self._names for seed in missing):
            self._last_list_read = self._clock()
            if not self._learn_from_list(list_address):
                self._list_address = None

    def _start_scan(self, seed: bytes, slot_seeds: list[bytes | None], want_cards: bool = True) -> None:
        if self.discovering or self._clock() < self._next_scan_at:
            return
        self._scan = threading.Thread(
            target=self._scan_in_background, args=(seed, slot_seeds, want_cards), name="dmgtracker-item-names", daemon=True
        )
        self._scan.start()

    # ------------------------------------------------------------------ learning

    def _learn_from_cards(self, header: int, slot_seeds: list[bytes | None]) -> bool:
        """Pair the cards at the remembered header with the slots; a pair becomes a name
        once the previous read had the same pair. False when the node is gone from there."""
        node = self._node_at(header)
        if node is None:
            return False
        data, count = node
        cards = self._cards_at(data, count)
        pairs = self._pair(slot_seeds, cards)
        cards_by_seed = self._pair_cards(slot_seeds, cards)
        with self._lock:
            for seed, name in pairs.items():
                if self._last_pairs.get(seed) == name:
                    self._names[seed] = name
                    self._cards[seed] = cards_by_seed[seed]
            self._last_pairs = pairs
        return True

    def _learn_slot_cards(self, header: int, slot_count: int) -> bool:
        """Read the cards at the remembered header as one card per weapon slot; a slot's
        card counts once two reads in a row agree on its name. False when the node is gone."""
        node = self._node_at(header)
        if node is None:
            return False
        data, count = node
        cards = [card for card in self._cards_at(data, count) if card is not None]
        if len(cards) != slot_count or any(not card.is_weapon or not card.name for card in cards):
            # A model in transition, or a count that does not match the slots: keep what we have.
            return True
        seen = dict(enumerate(cards))
        with self._lock:
            for index, card in seen.items():
                previous = self._last_slot_cards.get(index)
                if previous is not None and previous.name == card.name:
                    self._slot_cards[index] = card
            self._last_slot_cards = seen
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
                # A card of the equipped-weapon model knows the damage figure, the list
                # record the slugs: keep whichever fields each one has.
                self._cards[seed] = _merge_cards(self._cards.get(seed), card)
        return True

    def _pair(self, slot_seeds: list[bytes | None], cards: list[ItemCard | None]) -> dict[bytes, str]:
        """Card i describes the gun in slot i. Empty slots have no seed on one side and no
        valid card on the other, so both lists skip them; a count that still differs, or
        a card that is not a weapon's, means a model in transition and pairs nothing."""
        present = [card for card in cards if card is not None]
        if len(present) != len(slot_seeds):
            return {}
        pairs: dict[bytes, str] = {}
        for seed, card in zip(slot_seeds, present):
            if not card.is_weapon or not card.name:
                return {}
            if seed is not None:
                pairs[seed] = card.name
        return pairs

    def _pair_cards(self, slot_seeds: list[bytes | None], cards: list[ItemCard | None]) -> dict[bytes, ItemCard]:
        """The card in each slot, by the same pairing as the names."""
        present = [card for card in cards if card is not None]
        if len(present) != len(slot_seeds):
            return {}
        return {seed: card for seed, card in zip(slot_seeds, present) if seed is not None}

    # ------------------------------------------------------------------ scan thread

    def _scan_in_background(self, seed: bytes, slot_seeds: list[bytes | None], want_cards: bool = True) -> None:
        self.scans += 1
        started = time.perf_counter()
        what = "" if want_cards else " (the inventory list only)"
        wanted = f"the seed {seed.hex()[:6]}" if seed else "the item cards (no item ids in this session)"
        self._log(f"scan {self.scans} for {wanted}{what} started, {self._scan_order_text()} first")
        try:
            found = self._scan_for(seed, slot_seeds, want_cards)
        except Exception as ex:  # noqa: BLE001 - a scan that fails is a scan that found nothing
            self.last_error = f"{type(ex).__name__}: {ex}"
            found = False
        self.last_scan_s = time.perf_counter() - started
        if found:
            self._retry_s = SCAN_RETRY_FIRST_S
            self._log(f"scan {self.scans} done in {self.last_scan_s:.1f} s: {self._found_text()}")
            return
        self._next_scan_at = self._clock() + self._retry_s
        self._log(f"scan {self.scans} done in {self.last_scan_s:.1f} s: {self.last_error}; {self._found_text()}; next in {self._retry_s:.0f} s")
        self._retry_s = min(self._retry_s * 2, SCAN_RETRY_MAX_S)

    def _scan_order_text(self) -> str:
        if self._scan_hint is not None:
            return f"the regions around {self._scan_hint:#x}"
        return f"the regions in {DEFAULT_SCAN_BAND[0]:#x}-{DEFAULT_SCAN_BAND[1]:#x}"

    def _found_text(self) -> str:
        parts = []
        if self._list_address is not None:
            parts.append(f"inventory list at {self._list_address:#x}")
        if self._cards_header is not None:
            parts.append(f"item cards at {self._cards_header:#x}, {len(self._last_pairs)} pair(s) awaiting the confirming read")
        parts.append(f"{len(self._names)} name(s) known")
        return ", ".join(parts)

    def _scan_for(self, seed: bytes, slot_seeds: list[bytes | None], want_cards: bool = True) -> bool:
        """One pass over the heaps for the seed (an inventory record gives the name at
        once) and, when wanted, for the cards' node (its cards pair with the slots, the
        confirming read follows). True once the seed has a name or the cards were found."""
        node_needle = _search_needle(self.layout.cards_node.before)
        since_rest = 0
        for base, size in self._scan_order(list(self._regions())):
            for chunk_start in range(base, base + size, SCAN_CHUNK_BYTES):
                chunk = self._read(chunk_start, min(SCAN_CHUNK_BYTES + SCAN_OVERLAP_BYTES, base + size - chunk_start))
                if chunk is None:
                    continue
                # An empty seed means the caller has none (a co-op client): only the cards
                # are searched for, and `find` on an empty needle would never move on.
                position = chunk.find(seed) if seed else -1
                while 0 <= position < SCAN_CHUNK_BYTES:
                    if self._learn_list_around(chunk_start + position) and seed in self._names:
                        return True
                    position = chunk.find(seed, position + 1)
                position = chunk.find(node_needle) if want_cards else -1
                while 0 <= position < SCAN_CHUNK_BYTES:
                    if position % RECORD_ALIGNMENT == 0 and self._learn_cards_around(chunk, chunk_start, position, slot_seeds):
                        return True
                    position = chunk.find(node_needle, position + 1)
                since_rest += len(chunk)
                if since_rest >= SCAN_REST_EVERY_BYTES:
                    since_rest = 0
                    time.sleep(SCAN_REST_S)
        self.last_error = (
            "no item cards were found" if not seed
            else f"no UI model holds the seed {seed.hex()[:6]} and no item cards were found"
        )
        return False

    def _scan_order(self, regions: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """The regions near the last found model (or in the default band) first, the rest
        after them, each part in address order."""
        if self._scan_hint is not None:
            low, high = self._scan_hint - SCAN_HINT_RADIUS, self._scan_hint + SCAN_HINT_RADIUS
        else:
            low, high = DEFAULT_SCAN_BAND
        near = [region for region in regions if region[0] < high and region[0] + region[1] > low]
        far = [region for region in regions if not (region[0] < high and region[0] + region[1] > low)]
        return near + far

    def _learn_list_around(self, hit: int) -> bool:
        """A hit of the seed as an inventory record: the whole list's names are taken."""
        record = hit - self.layout.inventory.seed_offset
        if self._list_record_at(record) is None:
            return False
        list_address = self._list_start(record)
        self._list_address = list_address
        self._scan_hint = list_address
        self._last_list_read = self._clock()
        return self._learn_from_list(list_address)

    def _learn_cards_around(self, chunk: bytes, chunk_start: int, position: int, slot_seeds: list[bytes | None]) -> bool:
        """A hit of the node's leading bytes: the window around it must have the node's
        shape (checked on the chunk, no read), then the array it names must hold cards."""
        node_layout = self.layout.cards_node
        window = chunk[position:position + node_layout.window_bytes]
        if len(window) < node_layout.window_bytes:
            window = self._read_exact(chunk_start + position, node_layout.window_bytes)
            if window is None:
                return False
        node = self._node_in(window)
        if node is None:
            return False
        data, count = node
        if not any(card is not None for card in self._cards_at(data, count)):
            return False
        header = chunk_start + position + len(node_layout.before)
        self._cards_header = header
        self._scan_hint = header
        return self._learn_from_cards(header, slot_seeds)

    # ------------------------------------------------------------------ records

    def _node_at(self, header: int) -> tuple[int, int] | None:
        """The (data, count) of the cards array whose header is at the address, or None
        when the node around it does not have the node's shape any more."""
        node_layout = self.layout.cards_node
        window = self._read_exact(header - len(node_layout.before), node_layout.window_bytes)
        if window is None:
            return None
        return self._node_in(window)

    def _node_in(self, window: bytes) -> tuple[int, int] | None:
        """(data, count) when the window, starting at the preceding header, has the shape
        of the cards' node: the leading bytes, a plausible data pointer, a count of
        1..MAX_CARD_RECORDS with a capacity at most a few records larger, the tail bytes."""
        node_layout = self.layout.cards_node
        lead = len(node_layout.before)
        if window[:lead] != node_layout.before:
            return None
        data, count, capacity = _pointer_at(window, lead), _int_at(window, lead + 8), _int_at(window, lead + 12)
        if not _is_pointer(data) or count < 1 or count > MAX_CARD_RECORDS or capacity < count or capacity > count + node_layout.spare_records:
            return None
        tail_start = lead + node_layout.after_offset
        if window[tail_start:tail_start + len(node_layout.after)] != node_layout.after:
            return None
        return data, count

    def _list_start(self, record: int) -> int:
        stride = self.layout.inventory.record_bytes
        steps = 0
        while steps < MAX_LIST_RECORDS and self._list_record_at(record - stride) is not None:
            record -= stride
            steps += 1
        return record

    def _cards_at(self, address: int, count: int) -> list[ItemCard | None]:
        """The records of the cards array, None for one that does not validate (an empty
        slot's card, or a record in transition)."""
        return [self._card_at(address + index * self.layout.cards.record_bytes) for index in range(count)]

    def _list_at(self, address: int) -> dict[bytes, ItemCard]:
        entries: dict[bytes, ItemCard] = {}
        while len(entries) < MAX_LIST_RECORDS:
            entry = self._list_record_at(address + len(entries) * self.layout.inventory.record_bytes)
            if entry is None:
                break
            seed, card = entry
            entries[seed] = card
        return entries

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
        thumbnail = self._fstring(record, layout.thumbnail_offset, MAX_PATH_CHARS) or ""
        return ItemCard(slot_type, name.strip(), element, damage_text, serial, rarity, thumbnail)

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
        thumbnail = self._fstring(record, layout.thumbnail_offset, MAX_PATH_CHARS) or ""
        return seed, ItemCard(slot_type, name.strip(), element, "", serial, rarity, thumbnail)

    def _read_exact(self, address: int, size: int) -> bytes | None:
        """The bytes at address, or None unless all of them are readable."""
        data = self._read(address, size)
        if data is None or len(data) < size:
            return None
        return data

    def _fstring(self, record: bytes, offset: int, max_chars: int = MAX_NAME_CHARS) -> str | None:
        """The text of the FString {data, num, max} at offset; '' for an empty one, None
        when the header is not an FString or the text is not readable."""
        pointer, num, capacity = _pointer_at(record, offset), _int_at(record, offset + 8), _int_at(record, offset + 12)
        if num == 0 and capacity >= 0:
            return ""
        if not _is_pointer(pointer) or num < 1 or num > max_chars + 1 or capacity < num:
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
