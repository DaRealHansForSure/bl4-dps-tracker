"""The on-screen table row model: a fixed layout, nothing moves."""

from __future__ import annotations

import unittest

from dmgtracker.attribution import DamageSource
from dmgtracker.stats import EncounterTracker, RollingStats, source_key
from dmgtracker.table import (
    MAX_ROWS,
    PeakDps,
    STYLE_ACTIVE,
    STYLE_HEADER,
    STYLE_ROW,
    STYLE_SPACER,
    STYLE_TOTAL,
    build_rows,
    window_label,
)

SNIPER = DamageSource("weapon", "Jakobs Sniper", "Body_JAK_SR", 277)
RIFLE = DamageSource("weapon", "Vladof Assault Rifle", "Body_VLA_AR", 280)
EQUIPPED = [RIFLE, SNIPER]
WINDOW_MINUTES = 5.0
FIXED_LABELS = ["Weapon", "Vladof Assault Rifle", "Jakobs Sniper", "-", "-", "Skill", "Grenade", "Melee", "Other", "", "Last 5 min"]


def hit(t: float, dmg: float, source: DamageSource = SNIPER, kind: str = "direct") -> dict:
    return {"t": t, "dmg": dmg, "kind": kind, "crit": False, "kill": False,
            "src": source.kind, "weapon": source.label, "wid": source.instance_id, "target": "Char_X_1"}


def window_with(*events: dict, now: float) -> RollingStats:
    window = RollingStats(WINDOW_MINUTES * 60.0)
    for event in events:
        window.add(event)
    window.prune(now)
    return window


class WindowLabelTests(unittest.TestCase):
    def test_labels(self) -> None:
        self.assertEqual(window_label(5.0), "Last 5 min")
        self.assertEqual(window_label(2.5), "Last 2.5 min")
        self.assertEqual(window_label(0.5), "Last 30 s")


class BuildRowsTests(unittest.TestCase):
    def test_layout_is_fixed_even_without_hits(self) -> None:
        rows = build_rows(window_with(now=100.0), WINDOW_MINUTES, EQUIPPED, held_instance_id=280)
        self.assertEqual([row.cells[0] for row in rows], FIXED_LABELS)
        self.assertEqual(rows[0].style, STYLE_HEADER)
        self.assertEqual(rows[1].cells, ("Vladof Assault Rifle", "0", "0", "0.0"))
        self.assertEqual(rows[1].style, STYLE_ACTIVE)
        self.assertEqual(rows[2].style, STYLE_ROW)
        self.assertEqual(rows[3].cells, ("-", "0", "0", "0.0"))
        self.assertEqual(rows[5].cells, ("Skill", "0", "0", "0.0"))
        self.assertEqual(rows[9].style, STYLE_SPACER)
        self.assertEqual(rows[10].cells, ("Last 5 min", "0", "0", "0.0"))
        self.assertEqual(rows[10].style, STYLE_TOTAL)
        self.assertEqual(len(rows), MAX_ROWS - 1)

    def test_encounter_row_is_optional_and_last(self) -> None:
        tracker = EncounterTracker()
        tracker.add(hit(100.0, 400.0))
        rows = build_rows(window_with(now=100.0), WINDOW_MINUTES, EQUIPPED, 0, encounter=tracker.latest())
        self.assertEqual(len(rows), MAX_ROWS)
        self.assertEqual(rows[-1].cells, ("Encounter", "400", "400", "100.0"))
        self.assertEqual(rows[-1].style, STYLE_TOTAL)

    def test_weapon_dps_is_measured_over_time_in_hand(self) -> None:
        window = window_with(hit(100.0, 1_234_567.0, SNIPER), hit(104.0, 765_433.0, RIFLE), now=110.0)
        for step in range(40):
            window.add_held(100.0 + step * 0.25, source_key(hit(0.0, 0.0, SNIPER)), 0.25)
        window.prune(110.0)
        rows = build_rows(window, WINDOW_MINUTES, EQUIPPED, 277)
        # Sniper: 1'234'567 over 10 s in hand. Rifle: no held time, so over the 10 s since the first hit.
        self.assertEqual(rows[2].cells, ("Jakobs Sniper", "1'234'567", "123'457", "61.7"))
        self.assertEqual(rows[2].style, STYLE_ACTIVE)
        self.assertEqual(rows[1].cells, ("Vladof Assault Rifle", "765'433", "76'543", "38.3"))
        self.assertEqual(rows[10].cells, ("Last 5 min", "2'000'000", "200'000", "100.0"))

    def test_non_weapon_rows_keep_their_places_and_sum_their_kind(self) -> None:
        shotgun = DamageSource("weapon", "Torgue Shotgun", "Body_TOR_SG", 278)
        grenade = DamageSource("grenade", "Grenade", "", 0)
        melee = DamageSource("melee", "Melee", "", 0)
        skill = DamageSource("skill", "Skill", "", 0)
        window = window_with(
            hit(100.0, 100.0, shotgun), hit(101.0, 50.0, melee), hit(101.5, 333.0, grenade),
            hit(102.0, 25.0, SNIPER, kind="dot"), hit(103.0, 1000.0, skill), now=105.0,
        )
        rows = build_rows(window, WINDOW_MINUTES, EQUIPPED, 280)
        self.assertEqual([row.cells[0] for row in rows], FIXED_LABELS)
        self.assertEqual(rows[2].cells[1], "25", "the sniper's status ticks count for the sniper")
        self.assertEqual(rows[5].cells[1], "1'000", "Skill")
        self.assertEqual(rows[6].cells[1], "333", "Grenade")
        self.assertEqual(rows[7].cells[1], "50", "Melee")
        self.assertEqual(rows[8].cells[1], "0", "Other")
        self.assertEqual(rows[10].cells[1], "1'508", "the swapped-out shotgun still counts in the total")

    def test_best_dps_column_is_optional_and_covers_weapons_and_sources(self) -> None:
        best = PeakDps(weapons={277: 12345.6, 280: 0.0}, sources={"skill": 2000.4, "grenade": 0.0})
        rows = build_rows(window_with(now=100.0), WINDOW_MINUTES, EQUIPPED, 280, best=best)
        self.assertEqual(rows[0].cells, ("Weapon", "Total", "DPS", "Share [%]", "Best DPS"))
        self.assertTrue(all(len(row.cells) == 5 for row in rows))
        self.assertEqual(rows[1].cells[4], "", "no peak yet")
        self.assertEqual(rows[2].cells[4], "12'346")
        self.assertEqual(rows[5].cells[4], "2'000", "Skill")
        self.assertEqual(rows[6].cells[4], "", "Grenade has no peak yet")
        self.assertEqual(rows[8].cells[4], "", "Other never has a peak")
        self.assertEqual(rows[10].cells[4], "")
        without = build_rows(window_with(now=100.0), WINDOW_MINUTES, EQUIPPED, 280)
        self.assertTrue(all(len(row.cells) == 4 for row in without))

    def test_hidden_rows_are_left_out_but_still_count_in_the_total(self) -> None:
        melee = DamageSource("melee", "Melee", "", 0)
        window = window_with(hit(100.0, 50.0, melee), hit(101.0, 950.0, SNIPER), now=105.0)
        rows = build_rows(window, WINDOW_MINUTES, EQUIPPED, 280, hidden_kinds=frozenset({"melee"}))
        labels = [row.cells[0] for row in rows]
        self.assertEqual(labels, ["Weapon", "Vladof Assault Rifle", "Jakobs Sniper", "-", "-", "Skill", "Grenade", "Other", "", "Last 5 min"])
        self.assertEqual(rows[-1].cells[1], "1'000")
        self.assertEqual(rows[2].cells[3], "95.0", "the share is of the whole total, hidden damage included")

    def test_four_equipped_weapons_fill_the_slots(self) -> None:
        four = [DamageSource("weapon", f"Gun {index}", "", index) for index in range(1, 5)]
        rows = build_rows(window_with(now=100.0), WINDOW_MINUTES, four, 3)
        self.assertEqual([row.cells[0] for row in rows[1:5]], ["Gun 1", "Gun 2", "Gun 3", "Gun 4"])
        self.assertEqual(rows[3].style, STYLE_ACTIVE)


if __name__ == "__main__":
    unittest.main()
