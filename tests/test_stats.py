"""Encounter segmentation, per-source split, rolling window with time in hand."""

from __future__ import annotations

import unittest

from dmgtracker.stats import (
    ENCOUNTER_GAP_S,
    MIN_DURATION_S,
    EncounterTracker,
    RollingStats,
    source_key,
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

    def test_custom_gap(self) -> None:
        tracker = EncounterTracker(gap_s=15.0)
        tracker.add(hit(100.0, 10.0))
        tracker.add(hit(112.0, 5.0))
        self.assertEqual(len(tracker.all_encounters()), 1)

    def test_single_hit_has_minimum_duration(self) -> None:
        self.tracker.add(hit(100.0, 400.0))
        self.assertEqual(self.tracker.latest().duration(), MIN_DURATION_S)
        self.assertEqual(self.tracker.latest().dps(), 400.0)

    def test_healing_is_ignored(self) -> None:
        self.tracker.add(hit(100.0, 50.0, kind="heal"))
        self.assertIsNone(self.tracker.latest())

    def test_per_source_split_credits_dot_to_its_source(self) -> None:
        self.tracker.add(hit(100.0, 100.0))
        self.tracker.add(hit(101.0, 40.0, weapon="Vladof Assault Rifle", wid=280))
        self.tracker.add(hit(102.0, 7.0, kind="dot"))
        self.tracker.add(hit(103.0, 333.0, weapon="Other", wid=0, src="other"))
        encounter = self.tracker.latest()
        self.assertEqual(encounter.total, 480.0)
        self.assertEqual(encounter.dot_damage, 7.0)
        by_label = {totals.label: totals.damage for totals in encounter.by_source.values()}
        self.assertEqual(by_label, {"Jakobs Sniper": 107.0, "Vladof Assault Rifle": 40.0, "Other": 333.0})
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


class RollingStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = RollingStats(window_s=60.0)

    def test_sum_and_dps_over_the_window(self) -> None:
        self.window.add(hit(100.0, 600.0))
        self.window.add(hit(130.0, 600.0))
        self.window.prune(150.0)
        self.assertEqual(self.window.total, 1200.0)
        # 50 s since the first hit is shorter than the window, so that is the divisor.
        self.assertEqual(self.window.duration(), 50.0)
        self.assertEqual(self.window.dps(), 24.0)
        self.window.prune(200.0)
        self.assertEqual(self.window.duration(), 60.0)

    def test_old_hits_fall_out_with_their_source_totals(self) -> None:
        self.window.add(hit(100.0, 600.0))
        self.window.add(hit(130.0, 600.0, weapon="Vladof Assault Rifle", wid=280))
        self.window.prune(161.0)
        self.assertEqual(self.window.total, 600.0)
        self.assertEqual([totals.label for totals in self.window.by_source.values()], ["Vladof Assault Rifle"])
        self.window.prune(200.0)
        self.assertEqual(self.window.total, 0.0)
        self.assertEqual(self.window.by_source, {})
        self.assertEqual(self.window.dps(), 0.0)

    def test_healing_is_ignored_and_dot_is_credited_and_counted(self) -> None:
        self.window.add(hit(100.0, 50.0, kind="heal"))
        self.window.add(hit(101.0, 7.0, kind="dot"))
        self.window.add(hit(102.0, 100.0))
        self.window.prune(110.0)
        self.assertEqual(self.window.total, 107.0)
        self.assertEqual(self.window.dot_damage, 7.0)
        self.assertEqual(len(self.window.by_source), 1)
        self.assertEqual(next(iter(self.window.by_source.values())).damage, 107.0)
        self.window.prune(200.0)
        self.assertEqual(self.window.dot_damage, 0.0)

    def test_dps_in_hand_uses_held_time(self) -> None:
        sniper = source_key(hit(0.0, 0.0))
        self.window.add(hit(100.0, 1000.0))
        for step in range(8):
            self.window.add_held(100.0 + step * 0.25, sniper, 0.25)
        self.window.prune(110.0)
        totals = self.window.by_source[sniper]
        self.assertAlmostEqual(totals.held_seconds, 2.0)
        self.assertAlmostEqual(self.window.source_dps(totals), 500.0)

    def test_held_time_alone_keeps_a_source_and_ages_out(self) -> None:
        rifle = ("weapon", "Vladof Assault Rifle", 280)
        self.window.add_held(100.0, rifle, 0.25)
        self.window.prune(101.0)
        self.assertIn(rifle, self.window.by_source)
        self.assertEqual(self.window.by_source[rifle].damage, 0.0)
        self.window.prune(200.0)
        self.assertNotIn(rifle, self.window.by_source)

    def test_source_without_held_time_falls_back_to_window_duration(self) -> None:
        self.window.add(hit(100.0, 300.0, weapon="Other", wid=0, src="other"))
        self.window.prune(130.0)
        totals = self.window.by_source[("other", "Other", 0)]
        self.assertAlmostEqual(self.window.source_dps(totals), 10.0)


if __name__ == "__main__":
    unittest.main()
