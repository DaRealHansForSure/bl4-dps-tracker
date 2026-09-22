"""The dashboard's API payloads, computed from session files and the weapon store."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dmgtracker.report import read_session
from dmgtracker.webdata import (
    SessionCache,
    encounters_payload,
    hits_page,
    list_sessions,
    session_path,
    session_weapons_payload,
    weapons_payload,
)
from dmgtracker.weaponstats import WeaponStore


def hit(t: float, dmg: float, weapon: str = "Jakobs Sniper", wid: str = "seed277", src: str = "weapon",
        target: str = "Char_CatAdult_1", kind: str = "direct", crit: bool = False, kill: bool = False, elem: str = "") -> dict:
    return {"t": t, "dmg": dmg, "kind": kind, "crit": crit, "kill": kill, "elem": elem,
            "src": src, "weapon": weapon, "body": "", "wid": wid, "dtype": "Normal", "target": target}


def write_session(path: Path, started: float, sections: list[tuple[dict | None, list[dict]]]) -> None:
    lines = [json.dumps({"meta": {"mod": "dmgtracker", "schema": 2, "started": started, "encounter_gap_s": 15.0}})]
    for identity, events in sections:
        if identity is not None:
            lines.append(json.dumps({"character": identity, "t": started}))
        for event in events:
            lines.append(json.dumps(event))
        lines.append(json.dumps({"t": started + 1, "held": "seed277", "weapon": "Jakobs Sniper", "s": 2.0}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


ROGUE = {"key": "8243", "class": "Char_RoboDealer", "name": "RoboDealer"}
SIREN = {"key": "3ce4", "class": "Char_DarkSiren", "name": "DarkSiren"}


class WebDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp.name)
        self.old = self.log_dir / "session-20260920-100000.jsonl"
        self.new = self.log_dir / "session-20260920-120000.jsonl"
        write_session(self.old, 1000.0, [(SIREN, [hit(1001.0, 10.0, weapon="Vladof SMG", wid="seedA")])])
        write_session(self.new, 2000.0, [
            (ROGUE, [
                hit(2001.0, 100.0, crit=True),
                hit(2002.0, 50.0, weapon="Vladof Assault Rifle", wid="seed280", target="Char_Dog_2"),
                hit(2003.0, 333.0, weapon="Grenade", wid="", src="grenade", target="Char_Dog_2"),
                hit(2100.0, 900.0, kill=True, elem="Fire"),
            ]),
            (SIREN, [hit(2200.0, 5000.0, weapon="Skill", wid="", src="skill")]),
        ])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sessions_are_listed_newest_first_with_their_characters(self) -> None:
        sessions = list_sessions(self.log_dir)
        self.assertEqual([session["name"] for session in sessions], [self.new.name, self.old.name])
        self.assertEqual(sessions[0]["characters"], ["RoboDealer", "DarkSiren"])
        self.assertEqual(sessions[0]["started"], 2000.0)
        self.assertGreater(sessions[0]["bytes"], 0)
        self.assertEqual(list_sessions(self.log_dir / "missing"), [])

    def test_session_path_accepts_only_session_files_in_the_log_dir(self) -> None:
        self.assertEqual(session_path(self.log_dir, self.new.name), self.new)
        self.assertIsNone(session_path(self.log_dir, "session-20260920-999999.jsonl"))
        self.assertIsNone(session_path(self.log_dir, "..\\" + self.new.name))
        self.assertIsNone(session_path(self.log_dir, "../" + self.new.name))
        (self.log_dir / "notes.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(session_path(self.log_dir, "notes.txt"))

    def test_hits_page_is_newest_first_filtered_and_paged(self) -> None:
        session = read_session(self.new)
        page = hits_page(session, page=1, size=2, query="")
        self.assertEqual((page["total"], page["pages"], page["size"]), (5, 3, 2))
        self.assertEqual([event["t"] for event in page["events"]], [2200.0, 2100.0])
        self.assertEqual(page["events"][0]["character"], "DarkSiren")
        last = hits_page(session, page=3, size=2, query="")
        self.assertEqual([event["t"] for event in last["events"]], [2001.0])
        beyond = hits_page(session, page=9, size=2, query="")
        self.assertEqual(beyond["events"], [])
        dogs = hits_page(session, page=1, size=100, query="DOG")
        self.assertEqual([event["dmg"] for event in dogs["events"]], [333.0, 50.0])
        fire = hits_page(session, page=1, size=100, query="fire")
        self.assertEqual([event["dmg"] for event in fire["events"]], [900.0])
        by_character = hits_page(session, page=1, size=100, query="darksiren")
        self.assertEqual(by_character["total"], 1)
        session.sections[0].events[0]["why"] = "burning"
        by_rule = hits_page(session, page=1, size=100, query="burning")
        self.assertEqual(by_rule["total"], 1, "the hit lists can be filtered by the rule that claimed a hit")

    def test_hits_of_one_frame_keep_their_write_order_when_reversed(self) -> None:
        path = self.log_dir / "session-20260920-130000.jsonl"
        burst = [hit(3000.0, 1.0), hit(3000.0, 2.0), hit(3000.0, 3.0, kill=True)]
        write_session(path, 2900.0, [(ROGUE, burst)])
        page = hits_page(read_session(path), page=1, size=10, query="")
        self.assertEqual([event["dmg"] for event in page["events"]], [3.0, 2.0, 1.0], "the kill, written last, comes first")

    def test_weapons_payload_ranks_and_takes_the_top(self) -> None:
        store = WeaponStore(self.log_dir / "weapons.json")
        for index in range(12):
            store.add_event(hit(1.0, 100.0 * (index + 1), weapon=f"Gun {index}", wid=f"seed{index}"))
            store.add_held(f"seed{index}", f"Gun {index}", "", 10.0, 2.0)
        store.note_source_peak("skill", 1234.0, 60.0, 3.0)
        store.add_event(hit(1.0, 5000.0, weapon="Grenade", wid="nade", src="grenade"))
        store.add_throw("nade", "Grenade", 1.0)
        payload = weapons_payload(store, ROGUE, "damage", {"seed11": "Nightmarish Rangefinder", "nade": "Cheeky Fireball"})
        self.assertEqual(payload["by"], "damage")
        self.assertEqual(len(payload["weapons"]), 12, "the grenade is not among the weapons")
        self.assertEqual(len(payload["grenades"]), 1)
        grenade = payload["grenades"][0]
        self.assertEqual((grenade["rank"], grenade["kind"], grenade["name"], grenade["throws"], grenade["damage_per_throw"]), (1, "grenade", "Cheeky Fireball", 1, 5000.0))
        self.assertEqual(len(payload["top"]), 10)
        self.assertEqual((payload["top"][0]["label"], payload["top"][0]["name"]), ("Gun 11", "Nightmarish Rangefinder"))
        self.assertEqual(payload["top"][1]["name"], "")
        self.assertEqual(payload["top"][0]["rank"], 1)
        self.assertEqual(payload["top"][0]["dps_in_hand"], 120.0)
        self.assertEqual(payload["sources"], {"skill": 1234.0})
        self.assertEqual(payload["character"], ROGUE)
        self.assertEqual(weapons_payload(store, ROGUE, "nonsense")["by"], "dps")
        empty = weapons_payload(None, None, "dps")
        self.assertEqual((empty["weapons"], empty["top"], empty["grenades"], empty["sources"]), ([], [], [], {}))

    def test_session_weapons_payload(self) -> None:
        weapons = session_weapons_payload(read_session(self.new))
        self.assertEqual([weapon["label"] for weapon in weapons], ["Jakobs Sniper", "Vladof Assault Rifle"])
        self.assertEqual(weapons[0]["damage"], 1000.0)
        self.assertEqual(weapons[0]["held_seconds"], 2.0, "from the held line")
        self.assertEqual(weapons[0]["character"], "RoboDealer")

    def test_encounters_payload_splits_by_gap_and_source(self) -> None:
        payload = encounters_payload(read_session(self.new), gap_s=15.0)
        self.assertEqual(payload["gap_s"], 15.0)
        encounters = payload["encounters"]
        self.assertEqual([encounter["started"] for encounter in encounters], [2200.0, 2100.0, 2001.0])
        first = encounters[-1]
        self.assertEqual(first["total"], 483.0)
        self.assertEqual(first["hits"], 3)
        self.assertEqual(first["crits"], 1)
        self.assertEqual(first["sources"][0]["label"], "Grenade")
        self.assertEqual(first["targets"][0]["name"], "Char_Dog")
        self.assertEqual(encounters[1]["kills"], 1)
        self.assertEqual(encounters[0]["character"], "DarkSiren")

    def test_session_cache_reparses_only_when_the_file_changes(self) -> None:
        cache = SessionCache(capacity=1)
        first = cache.get(self.new)
        self.assertIs(cache.get(self.new), first)
        with self.new.open("a", encoding="utf-8") as file:
            file.write(json.dumps(hit(2300.0, 1.0)) + "\n")
        self.assertIsNot(cache.get(self.new), first)
        cache.get(self.old)
        self.assertIsNot(cache.get(self.new), first, "evicted by the capacity")


if __name__ == "__main__":
    unittest.main()
