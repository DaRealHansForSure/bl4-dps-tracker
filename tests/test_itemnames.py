"""The item card reader against a fake process memory, and the name matcher."""

from __future__ import annotations

import struct
import unittest

from dmgtracker.itemnames import (
    LATEST_LAYOUT,
    BuildLayout,
    ItemCard,
    ItemCardReader,
    NameMatcher,
    build_changelist,
    layout_for_build,
)

# Fake addresses, all above the reader's lowest plausible pointer.
GAME_INSTANCE = 0x100000
UI_OBJECT = 0x200000
POOL_BASE = 0x300000
POOL_SIZE = 0x40000
MODEL_TABLE = POOL_BASE + 0x1000
RECORDS = 0x400000
STRINGS = 0x500000


class FakeMemory:
    """A sparse process memory: bytes written at addresses, unreadable elsewhere."""

    def __init__(self) -> None:
        self.blocks: dict[int, bytearray] = {}
        self.regions: dict[int, tuple[int, int]] = {}

    def write(self, address: int, data: bytes) -> None:
        self.blocks[address] = bytearray(data)

    def read(self, address: int, size: int) -> bytes | None:
        for start, block in self.blocks.items():
            if start <= address and address + size <= start + len(block):
                return bytes(block[address - start:address - start + size])
        return None

    def region_of(self, address: int) -> tuple[int, int] | None:
        for base, size in self.regions.values():
            if base <= address < base + size:
                return (base, size)
        return None


class StringHeap:
    """Lays FString buffers out one after the other and hands out their headers."""

    def __init__(self, memory: FakeMemory, base: int) -> None:
        self.memory = memory
        self.next = base

    def fstring(self, text: str) -> bytes:
        if text == "":
            return struct.pack("<QII", 0, 0, 0)
        raw = text.encode("utf-16-le") + b"\x00\x00"
        address = self.next
        self.memory.write(address, raw)
        self.next += len(raw) + 16
        return struct.pack("<QII", address, len(text) + 1, len(text) + 1)


def make_record(heap: StringHeap, layout: BuildLayout, slot_type: str, name: str, serial: str, rarity: str, element: str = "corrosive", damage: str = "911") -> bytes:
    record = bytearray(layout.record_bytes)
    fields = {
        layout.slot_offset: slot_type,
        layout.name_offset: name,
        layout.element_offset: element,
        layout.damage_offset: damage,
        layout.serial_offset: serial,
        layout.rarity_offset: rarity,
    }
    for offset, text in fields.items():
        record[offset:offset + 16] = heap.fstring(text)
    return bytes(record)


def build_memory(records: list[bytes], layout: BuildLayout = LATEST_LAYOUT, header_at: int = POOL_BASE + 0x1240) -> FakeMemory:
    """Game instance -> UI object -> model table in a pool region; the records array's
    header sits somewhere in that region."""
    memory = FakeMemory()
    memory.write(GAME_INSTANCE + layout.game_instance_ui_offset, struct.pack("<Q", UI_OBJECT))
    memory.write(UI_OBJECT + layout.ui_model_table_offset, struct.pack("<Q", MODEL_TABLE))
    pool = bytearray(POOL_SIZE)
    pool[header_at - POOL_BASE:header_at - POOL_BASE + 16] = struct.pack("<QII", RECORDS, len(records), len(records))
    memory.write(POOL_BASE, bytes(pool))
    memory.regions["pool"] = (POOL_BASE, POOL_SIZE)
    memory.write(RECORDS, b"".join(records))
    return memory


class ItemCardReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = FakeMemory()
        self.heap = StringHeap(self.memory, STRINGS)

    def reader(self, memory: FakeMemory) -> ItemCardReader:
        return ItemCardReader(LATEST_LAYOUT, memory.read, memory.region_of)

    def test_reads_the_cards_in_slot_order(self) -> None:
        records = [
            make_record(self.heap, LATEST_LAYOUT, "weapon", "Domino Toochney Lucian's Flank ", "@Ugvelk3C0", "legendary"),
            make_record(self.heap, LATEST_LAYOUT, "weapon", "Maintained Screeching T.K's Wave ", "@Ugd_t@Fme", "legendary", "noelement", "1.612 x 10"),
        ]
        memory = build_memory(records)
        memory.blocks.update(self.memory.blocks)
        cards = self.reader(memory).cards(GAME_INSTANCE)
        self.assertEqual(
            cards,
            [
                ItemCard("weapon", "Domino Toochney Lucian's Flank", "corrosive", "911", "@Ugvelk3C0", "legendary"),
                ItemCard("weapon", "Maintained Screeching T.K's Wave", "noelement", "1.612 x 10", "@Ugd_t@Fme", "legendary"),
            ],
        )

    def test_the_header_is_remembered_and_rediscovered_when_it_moves(self) -> None:
        records = [make_record(self.heap, LATEST_LAYOUT, "weapon", "Lionized Parasite ", "@UgxFw!3FZ", "pearl")]
        memory = build_memory(records)
        memory.blocks.update(self.memory.blocks)
        reader = self.reader(memory)
        self.assertEqual([card.name for card in reader.cards(GAME_INSTANCE)], ["Lionized Parasite"])
        first_header = reader._header
        # The UI rebuilds its model: the old header is gone, a new one sits elsewhere.
        pool = memory.blocks[POOL_BASE]
        pool[first_header - POOL_BASE:first_header - POOL_BASE + 16] = bytes(16)
        pool[0x800:0x810] = struct.pack("<QII", RECORDS, 1, 1)
        self.assertEqual([card.name for card in reader.cards(GAME_INSTANCE)], ["Lionized Parasite"])
        self.assertEqual(reader._header, POOL_BASE + 0x800)

    def test_a_record_without_a_serial_is_not_a_card(self) -> None:
        records = [make_record(self.heap, LATEST_LAYOUT, "weapon", "Something", "not a serial", "epic")]
        memory = build_memory(records)
        memory.blocks.update(self.memory.blocks)
        reader = self.reader(memory)
        self.assertEqual(reader.cards(GAME_INSTANCE), [])
        self.assertIn("no item card array", reader.last_error)

    def test_a_missing_ui_pointer_gives_no_cards_and_an_error(self) -> None:
        reader = self.reader(FakeMemory())
        self.assertEqual(reader.cards(GAME_INSTANCE), [])
        self.assertEqual(reader.last_error, "no UI pointer in the game instance")

    def test_unreadable_name_text_fails_the_card(self) -> None:
        record = bytearray(make_record(self.heap, LATEST_LAYOUT, "weapon", "Ghost", "@Ug123", "rare"))
        # Point the name at memory that does not exist.
        record[LATEST_LAYOUT.name_offset:LATEST_LAYOUT.name_offset + 16] = struct.pack("<QII", 0x99999000, 6, 6)
        memory = build_memory([bytes(record)])
        memory.blocks.update(self.memory.blocks)
        self.assertEqual(self.reader(memory).cards(GAME_INSTANCE), [])


class NameMatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cards = [
            ItemCard("weapon", "Domino Toochney Lucian's Flank", "corrosive", "911", "@Ug1", "legendary"),
            ItemCard("weapon", "Maintained Screeching T.K's Wave", "noelement", "1.612 x 10", "@Ug2", "legendary"),
        ]

    def test_a_pairing_is_confirmed_by_the_second_read(self) -> None:
        matcher = NameMatcher()
        self.assertEqual(matcher.match(["ar", "sg"], self.cards), {})
        self.assertEqual(
            matcher.match(["ar", "sg"], self.cards),
            {"ar": "Domino Toochney Lucian's Flank", "sg": "Maintained Screeching T.K's Wave"},
        )
        self.assertTrue(matcher.confirmed(["ar", "sg"]))
        self.assertFalse(matcher.confirmed(["ar", "smg"]))

    def test_a_changed_pairing_starts_over(self) -> None:
        matcher = NameMatcher()
        matcher.match(["ar", "sg"], self.cards)
        swapped = [self.cards[1], self.cards[0]]
        self.assertEqual(matcher.match(["ar", "sg"], swapped), {}, "the cards lag the swap: nothing confirmed")
        self.assertEqual(matcher.match(["ar", "sg"], swapped), {"ar": "Maintained Screeching T.K's Wave", "sg": "Domino Toochney Lucian's Flank"})

    def test_a_count_mismatch_matches_nothing(self) -> None:
        matcher = NameMatcher()
        self.assertEqual(matcher.match(["ar"], self.cards), {})
        self.assertEqual(matcher.match(["ar"], self.cards), {})
        self.assertEqual(matcher.match([], self.cards), {})

    def test_non_weapon_cards_are_skipped(self) -> None:
        matcher = NameMatcher()
        cards = [ItemCard("shield", "Bubble", "", "", "@Ug9", "epic")] + self.cards
        matcher.match(["ar", "sg"], cards)
        self.assertEqual(len(matcher.match(["ar", "sg"], cards)), 2)


class BuildLayoutTest(unittest.TestCase):
    def test_changelist_parsing(self) -> None:
        self.assertEqual(build_changelist("Oak2-Win64-4845623-CL-4845623"), "4845623")
        self.assertEqual(build_changelist("Oak2-Win64-4845623"), "")
        self.assertEqual(build_changelist(""), "")

    def test_known_and_unknown_builds(self) -> None:
        layout, verified = layout_for_build("Oak2-Win64-4845623-CL-4845623")
        self.assertTrue(verified)
        self.assertEqual(layout.changelist, "4845623")
        layout, verified = layout_for_build("Oak2-Win64-9999999-CL-9999999")
        self.assertFalse(verified)
        self.assertIs(layout, LATEST_LAYOUT)


if __name__ == "__main__":
    unittest.main()
