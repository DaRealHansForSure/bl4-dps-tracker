"""
Offline report over one recorded session: encounters, session summary, weapons, last N seconds.

Runs outside the game with the standard library only:

    python dmgtracker/report.py                      newest session in the default log folder
    python dmgtracker/report.py path/to/session.jsonl
    python dmgtracker/report.py --last 300           average over the final five minutes
    python dmgtracker/report.py --gap 30             end an encounter after 30 s without a hit

The in-game `dmgreport` command runs the same functions on the current session and prints
to the SDK console. Encounters and the per-source split come from stats.py, so the report
and the live table agree by construction. Sessions are split at the character marker lines
the mod writes when the character changes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Run as a script from inside the package folder: make `dmgtracker` importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dmgtracker.formatting import format_duration, format_number, format_share  # noqa: E402
from dmgtracker.stats import ENCOUNTER_GAP_S, Encounter, EncounterTracker, target_family  # noqa: E402
from dmgtracker.weaponstats import WeaponRecord  # noqa: E402

DEFAULT_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "BL4DamageTracker" / "logs"
LATEST_POINTER_NAME = "latest-session.txt"
SESSION_GLOB = "session-*.jsonl"
TARGET_PREFIX = "Char_"
TOP_TARGETS = 3
SOURCE_WEAPON = "weapon"
KIND_DOT = "dot"


@dataclass
class CharacterSection:
    """The events played with one character; identity is None before the first marker."""

    identity: dict[str, str] | None
    events: list[dict[str, Any]] = field(default_factory=list)

    def title(self) -> str:
        if self.identity is None:
            return "Character: unknown"
        return f"Character: {self.identity.get('name', '?')} ({self.identity.get('class', '?')})"


@dataclass
class SessionWeapon:
    """A weapon's totals for the session plus how much of them was status damage."""

    record: WeaponRecord
    status_damage: float = 0.0


@dataclass
class Session:
    path: Path
    meta: dict[str, Any]
    sections: list[CharacterSection]

    def all_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for section in self.sections:
            events.extend(section.events)
        return events


# ------------------------------------------------------------------ reading

def default_session_path(log_dir: Path = DEFAULT_LOG_DIR) -> Path | None:
    """The session named in latest-session.txt, else the newest session file, else None."""
    pointer = log_dir / LATEST_POINTER_NAME
    if pointer.exists():
        candidate = log_dir / pointer.read_text(encoding="utf-8").strip()
        if candidate.exists():
            return candidate
    sessions = sorted(log_dir.glob(SESSION_GLOB))
    if sessions:
        return sessions[-1]
    return None


def read_session(path: Path) -> Session:
    """Parse a session file. Unreadable lines are skipped; a file being written to by the
    game may end in a partial line."""
    meta: dict[str, Any] = {}
    sections: list[CharacterSection] = [CharacterSection(None)]
    with path.open(encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if "meta" in record:
                meta = record["meta"]
            elif "character" in record:
                if sections[-1].events or sections[-1].identity is not None:
                    sections.append(CharacterSection(record["character"]))
                else:
                    sections[-1].identity = record["character"]
            elif "dmg" in record:
                sections[-1].events.append(record)
    for section in sections:
        section.events.sort(key=lambda event: float(event["t"]))
    return Session(path, meta, sections)


# ------------------------------------------------------------------ numbers

def encounters_for(events: list[dict[str, Any]], gap_s: float) -> list[Encounter]:
    tracker = EncounterTracker(gap_s=gap_s)
    for event in events:
        tracker.add(event)
    tracker.finish()
    return tracker.closed


def weapon_records_for(events: list[dict[str, Any]]) -> list[SessionWeapon]:
    """Per-weapon totals for the session, ranked by damage. The log has no time in hand, so
    held_seconds is the number of seconds in which the weapon landed a hit (active time)."""
    weapons: dict[int, SessionWeapon] = {}
    active_seconds: dict[int, set[int]] = {}
    for event in events:
        if event.get("src") != SOURCE_WEAPON:
            continue
        instance_id = int(event.get("wid", 0))
        weapon = weapons.get(instance_id)
        if weapon is None:
            record = WeaponRecord(instance_id, str(event.get("weapon", "Unknown")), str(event.get("body", "")), first_seen=float(event["t"]))
            weapon = SessionWeapon(record)
            weapons[instance_id] = weapon
            active_seconds[instance_id] = set()
        weapon.record.add(event)
        weapon.record.last_seen = float(event["t"])
        active_seconds[instance_id].add(int(float(event["t"])))
        if event.get("kind") == KIND_DOT:
            weapon.status_damage += float(event["dmg"])
    for instance_id, weapon in weapons.items():
        weapon.record.held_seconds = float(len(active_seconds[instance_id]))
    return sorted(weapons.values(), key=lambda weapon: weapon.record.damage, reverse=True)


def last_seconds_average(events: list[dict[str, Any]], seconds: float) -> tuple[float, float]:
    """(damage in the final `seconds` of the log, damage per second over that span)."""
    if not events or seconds <= 0:
        return 0.0, 0.0
    end = float(events[-1]["t"])
    total = sum(float(event["dmg"]) for event in events if float(event["t"]) >= end - seconds)
    return total, total / seconds


# ------------------------------------------------------------------ text

def format_report(session: Session, gap_s: float, last_seconds: float | None = None) -> list[str]:
    lines: list[str] = []
    events = session.all_events()
    started = session.meta.get("started")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(started))) if started else "unknown start"
    span = format_duration(float(events[-1]["t"]) - float(events[0]["t"])) if events else "0:00"
    lines.append(f"BL4 Damage Tracker report: {session.path.name} ({when}, {span} of hits, {format_number(len(events))} hits)")
    lines.append(f"encounter gap {format_number(gap_s)} s")
    for section in session.sections:
        if not section.events:
            continue
        lines.append("")
        lines.append(section.title())
        lines.extend(_format_encounters(section.events, gap_s))
        lines.extend(_format_weapons(section.events))
    if last_seconds:
        total, per_second = last_seconds_average(events, last_seconds)
        lines.append("")
        lines.append(f"Last {format_number(last_seconds)} s of the log: {format_number(total)} damage, {format_number(per_second)} per second")
    return lines


def _format_encounters(events: list[dict[str, Any]], gap_s: float) -> list[str]:
    encounters = encounters_for(events, gap_s)
    lines: list[str] = []
    for index, encounter in enumerate(encounters, start=1):
        started = time.strftime("%H:%M:%S", time.localtime(encounter.started))
        lines.append(
            f"  Encounter {index:>2}  {started}  {format_duration(encounter.duration()):>5}  "
            f"total {format_number(encounter.total):>12}  DPS {format_number(encounter.dps()):>9}  "
            f"active DPS {format_number(encounter.active_dps()):>9}  kills {encounter.kills:>3}  "
            f"crit {format_share(encounter.crits, encounter.hits):>5} %"
        )
        targets = ", ".join(f"{_target_label(name)} x{count}" for name, count in encounter.top_targets(TOP_TARGETS))
        lines.append(f"      targets: {targets}")
        for totals in sorted(encounter.by_source.values(), key=lambda item: item.damage, reverse=True):
            label = totals.label if totals.kind != SOURCE_WEAPON else f"{totals.label} #{totals.instance_id}"
            lines.append(f"      {label:28} {format_number(totals.damage):>12}  {format_share(totals.damage, encounter.total):>5} %")
    if not encounters:
        lines.append("  no encounters")
        return lines
    totals = [encounter.total for encounter in encounters]
    largest = max(range(len(totals)), key=lambda index: totals[index])
    lines.append(
        f"  Session: {len(encounters)} encounter(s), total {format_number(sum(totals))}, "
        f"average {format_number(sum(totals) / len(totals))}, largest {format_number(totals[largest])} (encounter {largest + 1})"
    )
    return lines


def _format_weapons(events: list[dict[str, Any]]) -> list[str]:
    weapons = weapon_records_for(events)
    if not weapons:
        return []
    lines = ["  Weapons (active DPS = damage over the seconds in which the gun landed hits):"]
    lines.append(f"    {'Weapon':26} {'Damage':>12} {'Active DPS':>11} {'Hits':>6} {'Avg hit':>9} {'Crit %':>7} {'Kills':>6} {'Status %':>9}")
    for weapon in weapons:
        record = weapon.record
        lines.append(
            f"    {record.label[:22] + ' #' + str(record.instance_id):26} {format_number(record.damage):>12} "
            f"{format_number(record.dps_in_hand()):>11} {record.hits:>6} {format_number(record.average_hit()):>9} "
            f"{record.crit_share():>7} {record.kills:>6} {format_share(weapon.status_damage, record.damage):>9}"
        )
    return lines


def _target_label(family: str) -> str:
    if family.startswith(TARGET_PREFIX):
        return family[len(TARGET_PREFIX):]
    return family


# ------------------------------------------------------------------ command line

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report over a BL4 Damage Tracker session log.")
    parser.add_argument("session", nargs="?", type=Path, help="session file (default: the newest one)")
    parser.add_argument("--last", type=float, default=None, metavar="SECONDS", help="also average the final N seconds of the log")
    parser.add_argument("--gap", type=float, default=None, metavar="SECONDS", help="seconds without a hit that end an encounter (default: the session's setting)")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="where the sessions are")
    return parser


def gap_for(session: Session, override: float | None) -> float:
    if override is not None:
        return override
    return float(session.meta.get("encounter_gap_s", ENCOUNTER_GAP_S))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.session or default_session_path(args.log_dir)
    if path is None or not path.exists():
        print(f"no session file found (looked in {args.log_dir})")
        return 1
    session = read_session(path)
    for line in format_report(session, gap_for(session, args.gap), args.last):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
