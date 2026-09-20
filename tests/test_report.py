"""The offline report: reading a session, encounters, weapons, the last N seconds, the text."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from dmgtracker.report import (
    default_session_path,
    encounters_for,
    format_report,
    gap_for,
    last_seconds_average,
    main,
    read_session,
    weapon_records_for,
)

META = {"meta": {"mod": "dmgtracker", "version": "1.0.0", "schema": 1, "started": 1000.0, "encounter_gap_s": 15.0}}
ROGUE = {"character": {"key": "aaaa", "class": "Char_RoboDealer", "name": "Rogue"}, "t": 1000.5}
SIREN = {"character": {"key": "bbbb", "class": "Char_DarkSiren", "name": "DarkSiren"}, "t": 1300.0}


def hit(t: float, dmg: float, weapon: str = "Jakobs Sniper", wid: int = 277, src: str = "weapon",
        kind: str = "direct", crit: bool = False, kill: bool = False, target: str = "Char_CatAdult_1") -> dict:
    return {"t": t, "dmg": dmg, "kind": kind, "crit": crit, "kill": kill, "elem": 0, "flags": 0,
            "src": src, "weapon": weapon, "body": "", "wid": wid, "dtype": "Normal", "target": target}


ROGUE_EVENTS = [
    hit(1001.0, 100.0, crit=True),
    hit(1003.0, 50.0, weapon="Vladof Assault Rifle", wid=280),
    hit(1004.0, 7.0, kind="dot"),
    hit(1005.0, 333.0, weapon="Grenade", wid=0, src="grenade", target="Char_CatYoung_2"),
    hit(1100.0, 400.0, kill=True, target="Char_Boss_9"),
    hit(1101.0, 600.0, target="Char_Boss_9"),
]
SIREN_EVENTS = [hit(1301.0, 5000.0, weapon="Skill", wid=0, src="skill")]


def write_session(directory: Path, name: str = "session-20260920-220000.jsonl", trailing_garbage: bool = False) -> Path:
    path = directory / name
    lines = [json.dumps(META), json.dumps(ROGUE)]
    lines += [json.dumps(event, separators=(",", ":")) for event in ROGUE_EVENTS]
    lines.append(json.dumps({"t": 1002.0, "held": 277, "weapon": "Jakobs Sniper", "s": 2.0}))
    lines.append(json.dumps(SIREN))
    lines += [json.dumps(event, separators=(",", ":")) for event in SIREN_EVENTS]
    text = "\n".join(lines) + "\n"
    if trailing_garbage:
        text += '{"t": 1400.0, "dmg": 1'
    path.write_text(text, encoding="utf-8")
    return path


class ReadSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.directory = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_sections_follow_the_character_markers(self) -> None:
        session = read_session(write_session(self.directory))
        self.assertEqual(session.meta["encounter_gap_s"], 15.0)
        self.assertEqual([section.identity["name"] for section in session.sections], ["Rogue", "DarkSiren"])
        self.assertEqual(len(session.sections[0].events), 6)
        self.assertEqual(len(session.sections[1].events), 1)
        self.assertEqual(len(session.all_events()), 7)
        self.assertEqual(len(session.sections[0].held), 1)

    def test_partial_last_line_is_skipped(self) -> None:
        session = read_session(write_session(self.directory, trailing_garbage=True))
        self.assertEqual(len(session.all_events()), 7)

    def test_events_before_any_marker_form_an_unknown_section(self) -> None:
        path = self.directory / "session-1.jsonl"
        path.write_text(json.dumps(META) + "\n" + json.dumps(hit(1001.0, 1.0)) + "\n", encoding="utf-8")
        session = read_session(path)
        self.assertIsNone(session.sections[0].identity)
        self.assertEqual(session.sections[0].title(), "Character: unknown")

    def test_default_session_path_prefers_the_pointer(self) -> None:
        older = write_session(self.directory, "session-20260920-100000.jsonl")
        newer = write_session(self.directory, "session-20260920-120000.jsonl")
        self.assertEqual(default_session_path(self.directory), newer)
        (self.directory / "latest-session.txt").write_text(older.name + "\n", encoding="utf-8")
        self.assertEqual(default_session_path(self.directory), older)
        self.assertIsNone(default_session_path(self.directory / "missing"))


class NumbersTests(unittest.TestCase):
    def test_encounters_use_the_gap(self) -> None:
        encounters = encounters_for(ROGUE_EVENTS, gap_s=15.0)
        self.assertEqual([round(e.total) for e in encounters], [490, 1000])
        self.assertEqual(encounters[1].kills, 1)
        self.assertEqual(encounters[1].top_targets(1), [("Char_Boss", 2)])
        self.assertEqual(len(encounters_for(ROGUE_EVENTS, gap_s=200.0)), 1)

    def test_weapon_records_rank_by_damage_and_count_status(self) -> None:
        weapons = weapon_records_for(ROGUE_EVENTS)
        self.assertEqual([w.record.label for w in weapons], ["Jakobs Sniper", "Vladof Assault Rifle"])
        sniper = weapons[0]
        self.assertEqual(sniper.record.damage, 1107.0)
        self.assertEqual(sniper.record.hits, 4)
        self.assertEqual(sniper.record.crits, 1)
        self.assertEqual(sniper.record.kills, 1)
        self.assertEqual(sniper.status_damage, 7.0)
        self.assertEqual(sniper.record.held_seconds, 4.0, "four distinct seconds with a hit")
        self.assertAlmostEqual(sniper.record.dps_in_hand(), 1107.0 / 4.0)

    def test_held_lines_give_dps_in_hand(self) -> None:
        held = [{"t": 1002.0, "held": 277, "weapon": "Jakobs Sniper", "s": 1.0}, {"t": 1003.0, "held": 277, "weapon": "Jakobs Sniper", "s": 0.5}]
        weapons = weapon_records_for(ROGUE_EVENTS, held)
        sniper = weapons[0]
        self.assertEqual(sniper.record.held_seconds, 1.5)
        self.assertAlmostEqual(sniper.record.dps_in_hand(), 1107.0 / 1.5)
        rifle = weapons[1]
        self.assertEqual(rifle.record.held_seconds, 1.0, "no held line: active seconds")

    def test_last_seconds_average(self) -> None:
        total, per_second = last_seconds_average(ROGUE_EVENTS, 5.0)
        self.assertEqual(total, 1000.0)
        self.assertEqual(per_second, 200.0)
        self.assertEqual(last_seconds_average([], 5.0), (0.0, 0.0))


class TextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.directory = Path(self._temp.name)
        self.session = read_session(write_session(self.directory))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_report_text(self) -> None:
        lines = format_report(self.session, gap_for(self.session, None), last_seconds=300.0)
        text = "\n".join(lines)
        self.assertIn("7 hits", lines[0])
        self.assertIn("encounter gap 15 s", lines[1])
        self.assertIn("Character: Rogue (Char_RoboDealer)", text)
        self.assertIn("Character: DarkSiren (Char_DarkSiren)", text)
        self.assertIn("Encounter  1", text)
        self.assertIn("Encounter  2", text)
        self.assertIn("targets: Boss x2", text)
        self.assertIn("Jakobs Sniper #277", text)
        self.assertIn("Grenade", text)
        self.assertIn("Session: 2 encounter(s), total 1'490", text)
        self.assertIn("Skill", text)
        self.assertIn("Last 300 s of the log: 6'490 damage, 22 per second", text)
        self.assertNotIn("—", text)

    def test_top_limits_the_encounters_printed(self) -> None:
        lines = format_report(self.session, 15.0, top_encounters=1)
        text = "\n".join(lines)
        self.assertIn("showing the 1 largest of 2 encounters", text)
        self.assertIn("targets: Boss x2", text, "the largest fight is printed")
        self.assertNotIn("targets: CatAdult x3", text, "the smaller fight is not")
        self.assertIn("Session: 2 encounter(s)", text, "the summary still covers every encounter")
        everything = "\n".join(format_report(self.session, 15.0, top_encounters=None))
        self.assertIn("targets: CatAdult x3", everything)

    def test_gap_override(self) -> None:
        self.assertEqual(gap_for(self.session, None), 15.0)
        self.assertEqual(gap_for(self.session, 30.0), 30.0)

    def test_main_with_a_file(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main([str(self.session.path)]), 0)
        self.assertIn("BL4 Damage Tracker report", output.getvalue())

    def test_main_without_sessions(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--log-dir", str(self.directory / "nothing")]), 1)
        self.assertIn("no session file found", output.getvalue())


if __name__ == "__main__":
    unittest.main()
