"""The item name table: one file for every character, saved on an interval, read-only for
the report."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dmgtracker.itemnamestore import ItemInfo, ItemNameStore, read_item_names


class ItemNameStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "item-names.json"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_names_are_kept_by_item_id_and_saved_on_the_interval(self) -> None:
        store = ItemNameStore(self.path, save_interval_s=30.0)
        self.assertEqual(store.name_for("ddbe5d"), "")
        self.assertTrue(store.set_name("ddbe5d", "Maintained Screeching T.K's Wave", "Jakobs Shotgun", 100.0))
        self.assertFalse(store.set_name("ddbe5d", "Maintained Screeching T.K's Wave", "Jakobs Shotgun", 101.0), "same name again")
        self.assertTrue(store.set_name("134844", "Lionized Parasite", "Vladof SMG", 102.0))
        self.assertEqual(store.names(), {"ddbe5d": "Maintained Screeching T.K's Wave", "134844": "Lionized Parasite"})
        self.assertTrue(store.save_if_due(now=110.0), "the first save is due at once")
        self.assertTrue(store.set_name("9bf02c", "Domino Toochney Lucian's Flank", "Vladof Assault Rifle", 115.0))
        self.assertFalse(store.save_if_due(now=120.0), "the interval has not passed")
        self.assertTrue(store.save_if_due(now=145.0))
        self.assertFalse(store.save_if_due(now=200.0), "nothing changed since")
        reloaded = ItemNameStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.name_for("ddbe5d"), "Maintained Screeching T.K's Wave")
        self.assertEqual(reloaded.count, 3)
        self.assertEqual(read_item_names(self.path), reloaded.names())

    def test_infos_merge_what_the_cards_and_the_list_know(self) -> None:
        store = ItemNameStore(self.path)
        self.assertTrue(store.set_info("ddbe5d", ItemInfo("Maintained Screeching T.K's Wave", "Jakobs Shotgun", "legendary", "noelement", "1.612 x 10", "", 100.0)))
        self.assertFalse(store.set_info("ddbe5d", ItemInfo("Maintained Screeching T.K's Wave", "", "", "", "", "@Ugd_t", 101.0)), "the same name with a field filled in is no new name")
        info = store.info_for("ddbe5d")
        self.assertEqual((info.rarity, info.damage, info.serial, info.seen), ("legendary", "1.612 x 10", "@Ugd_t", 101.0))
        self.assertEqual(store.infos()["ddbe5d"]["label"], "Jakobs Shotgun")
        self.assertTrue(store.save_if_due(now=200.0, force=True))
        reloaded = ItemNameStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.info_for("ddbe5d").serial, "@Ugd_t")

    def test_clear_forgets_everything_and_is_worth_a_save(self) -> None:
        store = ItemNameStore(self.path)
        store.set_name("ddbe5d", "Maintained Screeching T.K's Wave", "Jakobs Shotgun", 100.0)
        self.assertTrue(store.save_if_due(now=100.0, force=True))
        self.assertEqual(store.clear(), 1)
        self.assertEqual(store.clear(), 0)
        self.assertTrue(store.save_if_due(now=100.0, force=True))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["items"], {})

    def test_a_corrupt_file_is_set_aside_by_the_store_and_ignored_by_the_report(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(read_item_names(self.path), {})
        self.assertTrue(self.path.exists(), "the report never touches the file")
        store = ItemNameStore(self.path)
        store.load()
        self.assertIn("JSONDecodeError", store.load_error)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.glob("item-names.corrupt-*.json"))), 1)
        self.assertEqual(store.count, 0)

    def test_a_missing_file_is_an_empty_table(self) -> None:
        self.assertEqual(read_item_names(self.path), {})
        store = ItemNameStore(self.path)
        store.load()
        self.assertEqual((store.load_error, store.count), ("", 0))


if __name__ == "__main__":
    unittest.main()
