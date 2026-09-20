"""Encounter segmentation, per-source split, rolling window."""

from __future__ import annotations

import unittest

from dmgtracker.stats import (
    ENCOUNTER_GAP_S,
    MIN_DURATION_S,
    EncounterTracker,
    RollingWindow,
    target_family,
)


def hit(t: float, dmg: float, weapon: str = "Jakobs Sniper", wid: int = 277, src: str = "weapon",
        kind: str = "direct", crit: bool = False, kill: bool = False, target: str = "Char_CatAdult_1") -> dict:
    return {"t": t, "dmg": dmg, "kind": kind, "crit": crit, "kill": kill, "src": src,
            "weapon": weapon, "wid": wid, "target": target}


class TargetFamilyTests(unittest.TestCase):
    def test_numeric_suffix_is_stripped(self) -> None:
        self.assertEqual(target_family("Char_CatAdult_2147476409"), "Char_CatAdult")

    def test_names_without_suffix_stay(self) -> None:
        self.assertEqual(target_family("Char_CatAdult"), "Char_CatAdult")
        self.assertEqual(target_family("Boss"), "Boss")


class EncounterTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = EncounterTracker()

    def test_no_events_means_no_encounter(self) -> None:
        self.assertIsNone(self.tracker.latest())
        self.assertEqual(self.tracker.all_encounters(), [])

    def test_hits_within_the_gap_share_an_encounter(self) -> None:
        self.tracker.add(hit(100.0, 10.0))
        self.tracker.add(hit(104.0, 20.0, crit=True))
        self.tracker.add(hit(108.0, 30.0, kill=True))
        encounter = self.tracker.latest()
        self.assertEqual(len(self.tracker.all_encounters()), 1)
        self.assertEqual(encounter.total, 60.0)
        self.assertEqual(encounter.hits, 3)
        self.assertEqual(encounter.crits, 1)
        self.assertEqual(encounter.kills, 1)
        self.assertEqual(encounter.duration(), 8.0)
        self.assertEqual(encounter.dps(), 7.5)

    def test_a_gap_closes_the_encounter(self) -> None:
        self.tracker.add(hit(100.0, 10.0))
        self.tracker.add(hit(100.0 + ENCOUNTER_GAP_S + 0.1, 5.0))
        encounters = self.tracker.all_encounters()
        self.assertEqual([e.total for e in encounters], [10.0, 5.0])
        self.assertIs(self.tracker.latest(), encounters[1])

    def test_exactly_the_gap_still_belongs_to_the_encounter(self) -> None:
        self.tracker.add(hit(100.0, 10.0))
        self.tracker.add(hit(100.0 + ENCOUNTER_GAP_S, 5.0))
        self.assertEqual(len(self.tracker.all_encounters()), 1)

    def test_single_hit_has_minimum_duration(self) -> None:
        self.tracker.add(hit(100.0, 400.0))
        self.assertEqual(self.tracker.latest().duration(), MIN_DURATION_S)
        self.assertEqual(self.tracker.latest().dps(), 400.0)

    def test_healing_is_ignored(self) -> None:
        self.tracker.add(hit(100.0, 50.0, kind="heal"))
        self.assertIsNone(self.tracker.latest())

    def test_per_source_split_holds_dot_out(self) -> None:
        self.tracker.add(hit(100.0, 100.0))
        self.tracker.add(hit(101.0, 40.0, weapon="Vladof Assault Rifle", wid=280))
        self.tracker.add(hit(102.0, 7.0, kind="dot"))
        self.tracker.add(hit(103.0, 333.0, weapon="Other", wid=0, src="other"))
        encounter = self.tracker.latest()
        self.assertEqual(encounter.total, 480.0)
        self.assertEqual(encounter.dot_damage, 7.0)
        by_label = {totals.label: totals.damage for totals in encounter.by_source.values()}
        self.assertEqual(by_label, {"Jakobs Sniper": 100.0, "Vladof Assault Rifle": 40.0, "Other": 333.0})
        self.assertEqual(encounter.other_damage(), 333.0)

    def test_active_dps_counts_only_seconds_with_hits(self) -> None:
        self.tracker.add(hit(100.2, 10.0))
        self.tracker.add(hit(100.7, 10.0))
        self.tracker.add(hit(103.1, 10.0))
        encounter = self.tracker.latest()
        self.assertAlmostEqual(encounter.active_dps(), 15.0)

    def test_targets_are_counted_by_family(self) -> None:
        self.tracker.add(hit(100.0, 1.0, target="Char_CatAdult_11"))
        self.tracker.add(hit(100.5, 1.0, target="Char_CatAdult_12"))
        self.tracker.add(hit(101.0, 1.0, target="Char_CatYoung_3"))
        self.assertEqual(self.tracker.latest().top_targets(1), [("Char_CatAdult", 2)])

    def test_close_if_idle(self) -> None:
        self.tracker.add(hit(100.0, 10.0))
        self.assertFalse(self.tracker.close_if_idle(100.0 + ENCOUNTER_GAP_S))
        self.assertIsNotNone(self.tracker.current)
        self.assertTrue(self.tracker.close_if_idle(100.0 + ENCOUNTER_GAP_S + 0.1))
        self.assertIsNone(self.tracker.current)
        self.assertEqual(self.tracker.latest().total, 10.0)
        self.assertFalse(self.tracker.close_if_idle(200.0))

    def test_finish_closes_the_open_encounter(self) -> None:
        self.tracker.add(hit(100.0, 10.0))
        self.tracker.finish()
        self.assertIsNone(self.tracker.current)
        self.assertEqual(len(self.tracker.closed), 1)

    def test_history_is_bounded_when_asked(self) -> None:
        tracker = EncounterTracker(max_closed=2)
        for index in range(5):
            tracker.add(hit(100.0 + index * 100.0, 1.0))
        tracker.finish()
        self.assertEqual(len(tracker.closed), 2)
        self.assertEqual(tracker.closed_total, 5)
        self.assertEqual(tracker.closed[-1].started, 500.0)


class RollingWindowTests(unittest.TestCase):
    def test_sum_and_dps_over_the_window(self) -> None:
        window = RollingWindow(window_s=60.0)
        window.add(100.0, 600.0)
        window.add(130.0, 600.0)
        self.assertEqual(window.total_at(150.0), 1200.0)
        self.assertEqual(window.dps_at(150.0), 20.0)

    def test_old_hits_fall_out(self) -> None:
        window = RollingWindow(window_s=60.0)
        window.add(100.0, 600.0)
        window.add(130.0, 600.0)
        self.assertEqual(window.total_at(161.0), 600.0)
        self.assertEqual(window.total_at(200.0), 0.0)
        self.assertEqual(window.dps_at(200.0), 0.0)


if __name__ == "__main__":
    unittest.main()
