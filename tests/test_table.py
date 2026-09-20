"""The on-screen table row model."""

from __future__ import annotations

import unittest

from dmgtracker.attribution import DamageSource
from dmgtracker.stats import EncounterTracker
from dmgtracker.table import (
    MAX_ROWS,
    MAX_SOURCE_ROWS,
    STYLE_ACTIVE,
    STYLE_HEADER,
    STYLE_ROW,
    STYLE_SPACER,
    STYLE_TOTAL,
    build_rows,
)

SNIPER = DamageSource("weapon", "Jakobs Sniper", "Body_JAK_SR", 277)
RIFLE = DamageSource("weapon", "Vladof Assault Rifle", "Body_VLA_AR", 280)
EQUIPPED = [RIFLE, SNIPER]


def hit(t: float, dmg: float, source: DamageSource = SNIPER, kind: str = "direct") -> dict:
    return {"t": t, "dmg": dmg, "kind": kind, "crit": False, "kill": False,
            "src": source.kind, "weapon": source.label, "wid": source.instance_id, "target": "Char_X_1"}


class BuildRowsTests(unittest.TestCase):
    def test_without_an_encounter_every_equipped_weapon_shows_zero(self) -> None:
        rows = build_rows(None, EQUIPPED, held_instance_id=280, rolling_dps=0.0, show_rolling_row=True)
        self.assertEqual(rows[0].cells, ("Weapon", "Total", "DPS", "Share [%]"))
        self.assertEqual(rows[0].style, STYLE_HEADER)
        self.assertEqual(rows[1].cells, ("Vladof Assault Rifle", "0", "0", "0.0"))
        self.assertEqual(rows[1].style, STYLE_ACTIVE)
        self.assertEqual(rows[2].cells, ("Jakobs Sniper", "0", "0", "0.0"))
        self.assertEqual(rows[2].style, STYLE_ROW)
        self.assertEqual(rows[3].style, STYLE_SPACER)
        self.assertEqual(rows[4].cells, ("Encounter", "0", "0", "0.0"))
        self.assertEqual(rows[4].style, STYLE_TOTAL)
        self.assertEqual(rows[5].cells, ("Last 60 s", "", "0", ""))
        self.assertEqual(len(rows), 6)

    def test_rolling_row_can_be_left_out(self) -> None:
        rows = build_rows(None, EQUIPPED, 0, 0.0, show_rolling_row=False)
        self.assertEqual(rows[-1].cells[0], "Encounter")

    def test_numbers_are_swiss_formatted_and_shares_add_up(self) -> None:
        tracker = EncounterTracker()
        tracker.add(hit(100.0, 1_234_567.0, SNIPER))
        tracker.add(hit(104.0, 765_433.0, RIFLE))
        rows = build_rows(tracker.latest(), EQUIPPED, 277, rolling_dps=44_120.4, show_rolling_row=True)
        self.assertEqual(rows[1].cells, ("Vladof Assault Rifle", "765'433", "191'358", "38.3"))
        self.assertEqual(rows[2].cells, ("Jakobs Sniper", "1'234'567", "308'642", "61.7"))
        self.assertEqual(rows[2].style, STYLE_ACTIVE)
        self.assertEqual(rows[4].cells, ("Encounter", "2'000'000", "500'000", "100.0"))
        self.assertEqual(rows[5].cells, ("Last 60 s", "", "44'120", ""))

    def test_unequipped_other_and_dot_rows_follow_the_equipped_ones(self) -> None:
        shotgun = DamageSource("weapon", "Torgue Shotgun", "Body_TOR_SG", 278)
        other = DamageSource("other", "Other", "", 0)
        tracker = EncounterTracker()
        tracker.add(hit(100.0, 100.0, shotgun))
        tracker.add(hit(101.0, 50.0, other))
        tracker.add(hit(102.0, 25.0, SNIPER, kind="dot"))
        rows = build_rows(tracker.latest(), EQUIPPED, 280, 0.0, show_rolling_row=False)
        labels = [row.cells[0] for row in rows]
        self.assertEqual(labels, ["Weapon", "Vladof Assault Rifle", "Jakobs Sniper", "Torgue Shotgun", "Other", "DoT", "", "Encounter"])
        self.assertEqual(rows[3].cells[1], "100")
        self.assertEqual(rows[4].cells[1], "50")
        self.assertEqual(rows[5].cells[1], "25")
        self.assertEqual(rows[7].cells[1], "175")

    def test_row_count_never_exceeds_the_widget_capacity(self) -> None:
        tracker = EncounterTracker()
        many = [DamageSource("weapon", f"Gun {index}", "", index) for index in range(1, 12)]
        for index, gun in enumerate(many):
            tracker.add(hit(100.0 + index * 0.1, 1.0, gun))
        rows = build_rows(tracker.latest(), many[:4], 1, 0.0, show_rolling_row=True)
        source_rows = [row for row in rows if row.style in (STYLE_ROW, STYLE_ACTIVE)]
        self.assertEqual(len(source_rows), MAX_SOURCE_ROWS)
        self.assertLessEqual(len(rows), MAX_ROWS)
        self.assertEqual(rows[-2].cells[0], "Encounter")


if __name__ == "__main__":
    unittest.main()
