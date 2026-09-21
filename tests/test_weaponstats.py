"""The lifetime per-weapon record and its JSON persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dmgtracker.weaponstats import WeaponStore


def hit(t: float, dmg: float, wid: str = "277", weapon: str = "Jakobs Sniper", body: str = "Body_JAK_SR",
        src: str = "weapon", kind: str = "direct", crit: bool = False, kill: bool = False) -> dict:
    return {"t": t, "dmg": dmg, "kind": kind, "crit": crit, "kill": kill,
            "src": src, "weapon": weapon, "body": body, "wid": wid}


class WeaponStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "weapons.json"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_events_and_held_time_build_a_record(self) -> None:
        store = WeaponStore(self.path)
        store.add_event(hit(100.0, 400.0, crit=True))
        store.add_event(hit(101.0, 600.0, kill=True))
        store.add_held("277", "Jakobs Sniper", "Body_JAK_SR", 4.0, 101.0)
        record = store.records["277"]
        self.assertEqual(record.damage, 1000.0)
        self.assertEqual((record.hits, record.crits, record.kills), (2, 1, 1))
        self.assertEqual(record.held_seconds, 4.0)
        self.assertEqual(record.dps_in_hand(), 250.0)
        self.assertEqual(record.average_hit(), 500.0)
        self.assertEqual(record.crit_share(), "50.0")
        self.assertEqual((record.first_seen, record.last_seen), (100.0, 101.0))

    def test_crit_rate_is_over_direct_hits(self) -> None:
        store = WeaponStore(self.path)
        store.add_event(hit(1.0, 100.0, crit=True))
        store.add_event(hit(2.0, 100.0))
        store.add_event(hit(3.0, 5.0, kind="dot"))
        record = store.records["277"]
        self.assertEqual((record.hits, record.direct_hits, record.crits), (3, 2, 1))
        self.assertEqual(record.crit_rate(), 0.5)
        self.assertEqual(record.crit_share(), "50.0")
        self.assertTrue(store.save_if_due(now=100.0, force=True))
        reloaded = WeaponStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.records["277"].direct_hits, 2)

    def test_other_and_healing_are_not_weapon_records(self) -> None:
        store = WeaponStore(self.path)
        store.add_event(hit(100.0, 300.0, wid="", weapon="Other", body="", src="other"))
        store.add_event(hit(101.0, 30.0, kind="heal"))
        self.assertEqual(store.records, {})

    def test_unknown_label_is_replaced_by_the_real_one(self) -> None:
        store = WeaponStore(self.path)
        store.add_held("5", "Unknown", "", 0.25, 100.0)
        store.add_event(hit(101.0, 10.0, wid="5", weapon="Bor SMG", body="Body_BOR_SM"))
        self.assertEqual((store.records["5"].label, store.records["5"].body), ("Bor SMG", "Body_BOR_SM"))

    def test_save_and_load_round_trip(self) -> None:
        store = WeaponStore(self.path, save_interval_s=30.0)
        store.add_event(hit(100.0, 400.0))
        self.assertTrue(store.save_if_due(now=110.0))
        self.assertFalse(store.save_if_due(now=111.0), "nothing changed")
        store.add_held("277", "Jakobs Sniper", "Body_JAK_SR", 2.0, 120.0)
        self.assertFalse(store.save_if_due(now=120.0), "interval not over")
        self.assertTrue(store.save_if_due(now=120.0, force=True))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], 3)
        reloaded = WeaponStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.load_error, "")
        self.assertEqual(reloaded.records["277"].damage, 400.0)
        self.assertEqual(reloaded.records["277"].held_seconds, 2.0)

    def test_corrupt_file_is_set_aside(self) -> None:
        self.path.write_text("not json", encoding="utf-8")
        store = WeaponStore(self.path)
        store.load()
        self.assertNotEqual(store.load_error, "")
        self.assertEqual(store.records, {})
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.glob("weapons.corrupt-*.json"))), 1)

    def test_ranking_and_text(self) -> None:
        store = WeaponStore(self.path)
        store.add_event(hit(100.0, 1000.0, wid="1", weapon="Slow Gun"))
        store.add_held("1", "Slow Gun", "", 10.0, 100.0)
        store.add_event(hit(100.0, 1000.0, wid="2", weapon="Fast Gun"))
        store.add_held("2", "Fast Gun", "", 2.0, 100.0)
        store.add_event(hit(100.0, 5.0, wid="3", weapon="Never Held"))
        self.assertEqual([record.label for record in store.ranking()], ["Fast Gun", "Slow Gun", "Never Held"])
        store.note_peak("1", "Slow Gun", "", dps=900.0, window_seconds=20.0, now=1.0)
        self.assertEqual([record.label for record in store.ranking(by="peak")][0], "Slow Gun")
        store.add_event(hit(101.0, 5000.0, wid="3", weapon="Never Held"))
        self.assertEqual([record.label for record in store.ranking(by="damage")][0], "Never Held")
        lines = store.format_ranking(limit=2)
        self.assertEqual(len(lines), 3)
        self.assertIn("Fast Gun #2", lines[1])
        self.assertIn("500", lines[1])

    def test_the_ranking_names_the_guns_the_table_knows(self) -> None:
        store = WeaponStore(self.path, save_interval_s=30.0)
        store.add_held("ddbe5d", "Jakobs Shotgun", "Body_JAK_SG", 2.0, 100.0)
        store.add_held("other", "Vladof SMG", "Body_VLA_SM", 2.0, 100.0)
        self.assertEqual(store.records["ddbe5d"].display_label(), "Jakobs Shotgun")
        self.assertEqual(store.records["ddbe5d"].display_label("Maintained Screeching T.K's Wave"), "Maintained Screeching T.K's Wave")
        lines = store.format_ranking(names={"ddbe5d": "Maintained Screeching T.K's Wave"})
        self.assertIn("Maintained Screeching  ddbe5d", "".join(lines), "22 characters of the name, then the id")
        self.assertIn("Vladof SMG other", "".join(lines), "a gun without a name keeps its label")

    def test_a_record_written_with_a_name_still_loads(self) -> None:
        # Records of version 1.2.0 carried the name; the table holds it since.
        store = WeaponStore(self.path)
        store.add_held("ddbe5d", "Jakobs Shotgun", "Body_JAK_SG", 2.0, 100.0)
        self.assertTrue(store.save_if_due(now=200.0, force=True))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["weapons"][0]["name"] = "Maintained Screeching T.K's Wave"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        reloaded = WeaponStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.records["ddbe5d"].label, "Jakobs Shotgun")
        self.assertFalse(hasattr(reloaded.records["ddbe5d"], "name"))

    def test_peak_dps_needs_a_long_enough_window_and_only_rises(self) -> None:
        store = WeaponStore(self.path)
        store.note_peak("277", "Jakobs Sniper", "Body_JAK_SR", dps=9000.0, window_seconds=3.0, now=100.0)
        self.assertNotIn("277", store.records, "three seconds of fighting is not a window")
        store.note_peak("277", "Jakobs Sniper", "Body_JAK_SR", dps=1200.0, window_seconds=12.0, now=101.0)
        store.note_peak("277", "Jakobs Sniper", "Body_JAK_SR", dps=800.0, window_seconds=30.0, now=102.0)
        record = store.records["277"]
        self.assertEqual((record.peak_dps, record.peak_dps_at), (1200.0, 101.0))
        store.save_if_due(now=200.0, force=True)
        reloaded = WeaponStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.records["277"].peak_dps, 1200.0)
        self.assertIn("Peak DPS", store.format_ranking()[0])
        self.assertIn("1'200", store.format_ranking()[1])

    def test_source_peaks_need_a_long_enough_window_and_only_rise(self) -> None:
        store = WeaponStore(self.path)
        store.note_source_peak("skill", dps=5000.0, window_seconds=4.0, now=100.0)
        self.assertEqual(store.source_peaks(), {}, "four seconds of fighting is not a window")
        store.note_source_peak("skill", dps=1500.0, window_seconds=12.0, now=101.0)
        store.note_source_peak("skill", dps=900.0, window_seconds=60.0, now=102.0)
        store.note_source_peak("grenade", dps=0.0, window_seconds=60.0, now=102.0)
        store.note_source_peak("other", dps=9999.0, window_seconds=60.0, now=102.0)
        self.assertEqual(store.source_peaks(), {"skill": 1500.0})
        self.assertEqual(store.source_peaks_by_kind["skill"].peak_dps_at, 101.0)
        self.assertTrue(store.save_if_due(now=200.0, force=True))
        reloaded = WeaponStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.source_peaks(), {"skill": 1500.0})
        expected_line = "    " + "Skill".ljust(24) + " peak DPS " + "1'500".rjust(12)
        self.assertEqual(reloaded.format_source_peaks(), [expected_line])
        reloaded.reset()
        self.assertEqual(reloaded.source_peaks(), {})

    def test_peaks_map(self) -> None:
        store = WeaponStore(self.path)
        store.note_peak("1", "A", "", dps=100.0, window_seconds=20.0, now=1.0)
        store.add_event(hit(2.0, 5.0, wid="2", weapon="B"))
        self.assertEqual(store.peaks(), {"1": 100.0})

    def test_records_without_peak_fields_still_load(self) -> None:
        self.path.write_text(json.dumps({"schema": 3, "weapons": [{"item_id": "abc", "label": "Old", "body": "", "damage": 10.0}]}), encoding="utf-8")
        store = WeaponStore(self.path)
        store.load()
        self.assertEqual(store.records["abc"].peak_dps, 0.0)
        self.assertEqual(store.source_peaks(), {})

    def test_schema_2_keeps_the_records_but_drops_the_in_hand_peaks(self) -> None:
        self.path.write_text(json.dumps({"schema": 2, "weapons": [{"item_id": "abc", "label": "Old", "body": "", "damage": 10.0, "hits": 2, "peak_dps": 500.0, "peak_dps_at": 9.0}]}), encoding="utf-8")
        store = WeaponStore(self.path)
        store.load()
        self.assertEqual((store.records["abc"].damage, store.records["abc"].hits), (10.0, 2))
        self.assertEqual((store.records["abc"].peak_dps, store.records["abc"].peak_dps_at), (0.0, 0.0))
        self.assertEqual(store.peaks_dropped, 1)
        self.assertTrue(store.save_if_due(now=100.0, force=True), "the dropped peaks are written back")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["schema"], 3)

    def test_schema_1_file_is_set_aside_because_its_ids_were_unstable(self) -> None:
        self.path.write_text(json.dumps({"schema": 1, "weapons": [{"instance_id": 5, "label": "Old", "body": "", "damage": 10.0}]}), encoding="utf-8")
        store = WeaponStore(self.path)
        store.load()
        self.assertEqual(store.records, {})
        self.assertEqual(store.load_error, "")
        self.assertIsNotNone(store.set_aside_old_schema)
        self.assertFalse(self.path.exists())
        self.assertIn("schema1", store.set_aside_old_schema.name)

    def test_reset_sets_the_file_aside_and_empties_the_records(self) -> None:
        store = WeaponStore(self.path)
        store.add_event(hit(100.0, 400.0))
        store.save_if_due(now=100.0, force=True)
        set_aside = store.reset()
        self.assertEqual(store.records, {})
        self.assertIsNotNone(set_aside)
        self.assertTrue(set_aside.exists())
        self.assertIn("weapons.reset-", set_aside.name)
        self.assertFalse(self.path.exists())
        self.assertTrue(store.save_if_due(now=200.0, force=True), "the empty record is saved")
        self.assertTrue(self.path.exists())

    def test_reset_without_a_file(self) -> None:
        store = WeaponStore(self.path)
        self.assertIsNone(store.reset())

    def test_empty_ranking_text(self) -> None:
        lines = WeaponStore(self.path).format_ranking()
        self.assertEqual(lines[-1], "no weapon data yet")


if __name__ == "__main__":
    unittest.main()
