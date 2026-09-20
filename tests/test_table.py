"""The on-screen table row model."""

from __future__ import annotations

import unittest

from dmgtracker.attribution import DamageSource
from dmgtracker.stats import EncounterTracker, RollingStats, source_key
from dmgtracker.table import (
    MAX_ROWS,
    MAX_SOURCE_ROWS,
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
    def test_without_hits_every_equipped_weapon_shows_zero(self) -> None:
        rows = build_rows(window_with(now=100.0), WINDOW_MINUTES, EQUIPPED, held_instance_id=280)
        self.assertEqual(rows[0].cells, ("Weapon", "Total", "DPS", "Share [%]"))
        self.assertEqual(rows[0].style, STYLE_HEADER)
        self.assertEqual(rows[1].cells, ("Vladof Assault Rifle", "0", "0", "0.0"))
        self.assertEqual(rows[1].style, STYLE_ACTIVE)
        self.assertEqual(rows[2].cells, ("Jakobs Sniper", "0", "0", "0.0"))
        self.assertEqual(rows[2].style, STYLE_ROW)
        self.assertEqual(rows[3].style, STYLE_SPACER)
        self.assertEqual(rows[4].cells, ("Last 5 min", "0", "0", "0.0"))
        self.assertEqual(rows[4].style, STYLE_TOTAL)
        self.assertEqual(len(rows), 5)

    def test_encounter_row_is_optional(self) -> None:
        tracker = EncounterTracker()
        tracker.add(hit(100.0, 400.0))
        rows = build_rows(window_with(now=100.0), WINDOW_MINUTES, EQUIPPED, 0, encounter=tracker.latest())
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
        self.assertEqual(rows[4].cells, ("Last 5 min", "2'000'000", "200'000", "100.0"))

    def test_unequipped_other_and_dot_rows_follow_the_equipped_ones(self) -> None:
        shotgun = DamageSource("weapon", "Torgue Shotgun", "Body_TOR_SG", 278)
        other = DamageSource("other", "Other", "", 0)
        window = window_with(
            hit(100.0, 100.0, shotgun), hit(101.0, 50.0, other), hit(102.0, 25.0, SNIPER, kind="dot"), now=105.0
        )
        rows = build_rows(window, WINDOW_MINUTES, EQUIPPED, 280)
        labels = [row.cells[0] for row in rows]
        self.assertEqual(
            labels, ["Weapon", "Vladof Assault Rifle", "Jakobs Sniper", "Torgue Shotgun", "Other", "DoT", "", "Last 5 min"]
        )
        self.assertEqual(rows[3].cells[1], "100")
        self.assertEqual(rows[4].cells[1], "50")
        self.assertEqual(rows[5].cells[1], "25")
        self.assertEqual(rows[7].cells[1], "175")

    def test_row_count_never_exceeds_the_widget_capacity(self) -> None:
        many = [DamageSource("weapon", f"Gun {index}", "", index) for index in range(1, 12)]
        events = [hit(100.0 + index * 0.1, 1.0, gun) for index, gun in enumerate(many)]
        window = window_with(*events, now=105.0)
        tracker = EncounterTracker()
        for event in events:
            tracker.add(event)
        rows = build_rows(window, WINDOW_MINUTES, many[:4], 1, encounter=tracker.latest())
        source_rows = [row for row in rows if row.style in (STYLE_ROW, STYLE_ACTIVE)]
        self.assertEqual(len(source_rows), MAX_SOURCE_ROWS)
        self.assertLessEqual(len(rows), MAX_ROWS)
        self.assertEqual(rows[-2].cells[0], "Last 5 min")
        self.assertEqual(rows[-1].cells[0], "Encounter")


if __name__ == "__main__":
    unittest.main()
