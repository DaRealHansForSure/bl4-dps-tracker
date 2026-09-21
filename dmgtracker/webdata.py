"""
What the dashboard's HTTP API answers, computed from the session files and the weapon
store: the session list, one page of hits with a text filter, the weapon ranking, the
encounters of a session. Plain functions over paths and objects, so the tests run them
without a server, and the server module stays about sockets only.

Session files are parsed on request; a small cache keyed by the file's size and
modification time saves the parse while a page is browsed. No SDK imports.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .attribution import SOURCE_GRENADE, SOURCE_WEAPON
from .report import SESSION_GLOB, encounters_for, read_session, weapon_records_for
from .stats import Encounter
from .weaponstats import WeaponRecord, WeaponStore

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
TOP_WEAPONS = 10
TOP_TARGETS = 3
CACHED_SESSIONS = 4
# The event fields a search term is matched against, case-insensitively.
SEARCH_FIELDS = ("weapon", "target", "src", "dtype", "elem", "kind", "character")
RANKINGS = ("dps", "peak", "damage")


class SessionCache:
    """Parsed sessions by path, dropped when the file changes or the cache fills up."""

    def __init__(self, capacity: int = CACHED_SESSIONS) -> None:
        self.capacity = capacity
        self._sessions: OrderedDict[Path, tuple[tuple[int, int], Any]] = OrderedDict()

    def get(self, path: Path) -> Any:
        stat = path.stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
        cached = self._sessions.get(path)
        if cached is not None and cached[0] == stamp:
            self._sessions.move_to_end(path)
            return cached[1]
        session = read_session(path)
        self._sessions[path] = (stamp, session)
        self._sessions.move_to_end(path)
        while len(self._sessions) > self.capacity:
            self._sessions.popitem(last=False)
        return session


def session_path(log_dir: Path, name: str) -> Path | None:
    """The session file for a name from the API, or None for anything that is not one of the
    session files in the log directory (no paths, no other files)."""
    if name != Path(name).name or not Path(name).match(SESSION_GLOB):
        return None
    path = log_dir / name
    if not path.is_file():
        return None
    return path


def list_sessions(log_dir: Path) -> list[dict[str, Any]]:
    """Every session file, newest first, with its size, start time and characters. Only the
    marker lines are read, so this is cheap even for many files."""
    sessions: list[dict[str, Any]] = []
    if not log_dir.is_dir():
        return sessions
    for path in sorted(log_dir.glob(SESSION_GLOB), reverse=True):
        stat = path.stat()
        started, characters = _session_markers(path)
        sessions.append({
            "name": path.name,
            "bytes": stat.st_size,
            "modified": stat.st_mtime,
            "started": started,
            "characters": characters,
        })
    return sessions


def _session_markers(path: Path) -> tuple[float, list[str]]:
    started = 0.0
    characters: list[str] = []
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                if line.startswith('{"meta"'):
                    started = float(json.loads(line)["meta"].get("started", 0.0))
                elif line.startswith('{"character"'):
                    name = str(json.loads(line)["character"].get("name", "?"))
                    if name not in characters:
                        characters.append(name)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return started, characters


def hits_page(session: Any, page: int, size: int, query: str) -> dict[str, Any]:
    """One page of the session's hits, newest first, filtered by a search term over the
    SEARCH_FIELDS. Pages count from 1; an out-of-range page gives an empty list."""
    size = max(1, min(int(size), MAX_PAGE_SIZE))
    page = max(1, int(page))
    events = _events_with_character(session)
    needle = query.strip().lower()
    if needle:
        events = [event for event in events if _matches(event, needle)]
    # Hits of one frame share a timestamp; the file order is the write order, so the hit
    # written last (the kill of a burst) comes first once the list is reversed.
    numbered = list(enumerate(events))
    numbered.sort(key=lambda pair: (float(pair[1]["t"]), pair[0]), reverse=True)
    events = [event for _order, event in numbered]
    total = len(events)
    pages = max(1, math.ceil(total / size))
    start = (page - 1) * size
    return {
        "session": session.path.name,
        "total": total,
        "page": page,
        "pages": pages,
        "size": size,
        "events": events[start : start + size],
    }


def _events_with_character(session: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for section in session.sections:
        name = "" if section.identity is None else str(section.identity.get("name", ""))
        for event in section.events:
            events.append({**event, "character": name})
    return events


def _matches(event: dict[str, Any], needle: str) -> bool:
    for field_name in SEARCH_FIELDS:
        value = event.get(field_name)
        if value is not None and needle in str(value).lower():
            return True
    return False


def weapons_payload(store: WeaponStore | None, character: dict[str, str] | None, by: str, names: dict[str, str] | None = None) -> dict[str, Any]:
    """The character's lifetime records, ranked: the guns (with the top ten of them), the
    grenades, and the peaks of the non-weapon sources; `names` (item id -> item name)
    labels the items whose name is known."""
    if by not in RANKINGS:
        by = RANKINGS[0]
    names = names or {}
    weapons: list[dict[str, Any]] = []
    grenades: list[dict[str, Any]] = []
    sources: dict[str, float] = {}
    if store is not None:
        weapons = _ranked_records(store, by, SOURCE_WEAPON, names)
        grenades = _ranked_records(store, by, SOURCE_GRENADE, names)
        sources = store.source_peaks()
    return {"character": character, "by": by, "top": weapons[:TOP_WEAPONS], "weapons": weapons, "grenades": grenades, "sources": sources}


def _ranked_records(store: WeaponStore, by: str, kind: str, names: dict[str, str]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for position, record in enumerate(store.ranking(by, kind), start=1):
        ranked.append(_record_json(position, record, names.get(record.item_id, "")))
    return ranked


def _record_json(position: int, record: WeaponRecord, name: str) -> dict[str, Any]:
    return {
        "rank": position,
        "kind": record.kind,
        "item_id": record.item_id,
        "label": record.label,
        "name": name,
        "body": record.body,
        "damage": record.damage,
        "hits": record.hits,
        "direct_hits": record.direct_hits,
        "crits": record.crits,
        "kills": record.kills,
        "held_seconds": record.held_seconds,
        "dps_in_hand": record.dps_in_hand(),
        "average_hit": record.average_hit(),
        "crit_share": record.crit_rate(),
        "peak_dps": record.peak_dps,
        "peak_dps_at": record.peak_dps_at,
        "first_seen": record.first_seen,
        "last_seen": record.last_seen,
        "throws": record.throws,
        "damage_per_throw": record.damage_per_throw(),
    }


def session_weapons_payload(session: Any, names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Per-weapon totals of one session (from the report), one list over all characters,
    named by item where `names` knows the gun."""
    weapons: list[dict[str, Any]] = []
    names = names or {}
    for section in session.sections:
        name = "" if section.identity is None else str(section.identity.get("name", ""))
        for weapon in weapon_records_for(section.events, section.held):
            record = weapon.record
            weapons.append({
                "character": name,
                "item_id": record.item_id,
                "label": record.label,
                "name": names.get(record.item_id, ""),
                "damage": record.damage,
                "hits": record.hits,
                "crits": record.crits,
                "kills": record.kills,
                "held_seconds": record.held_seconds,
                "dps_in_hand": record.dps_in_hand(),
                "average_hit": record.average_hit(),
                "status_damage": weapon.status_damage,
            })
    weapons.sort(key=lambda weapon: weapon["damage"], reverse=True)
    return weapons


def encounters_payload(session: Any, gap_s: float) -> dict[str, Any]:
    """The session's encounters, newest first, with their per-source split and top targets."""
    encounters: list[dict[str, Any]] = []
    for section in session.sections:
        name = "" if section.identity is None else str(section.identity.get("name", ""))
        for encounter in encounters_for(section.events, gap_s):
            encounters.append(_encounter_json(encounter, name))
    encounters.sort(key=lambda encounter: encounter["started"], reverse=True)
    return {"session": session.path.name, "gap_s": gap_s, "encounters": encounters}


def _encounter_json(encounter: Encounter, character: str) -> dict[str, Any]:
    sources = sorted(encounter.by_source.values(), key=lambda totals: totals.damage, reverse=True)
    return {
        "character": character,
        "started": encounter.started,
        "ended": encounter.last_hit,
        "duration": encounter.duration(),
        "total": encounter.total,
        "dps": encounter.dps(),
        "active_dps": encounter.active_dps(),
        "hits": encounter.hits,
        "crits": encounter.crits,
        "kills": encounter.kills,
        "dot_damage": encounter.dot_damage,
        "sources": [
            {"kind": totals.kind, "label": totals.label, "item_id": totals.item_id, "damage": totals.damage, "hits": totals.hits}
            for totals in sources
        ],
        "targets": [{"name": target, "hits": count} for target, count in encounter.top_targets(TOP_TARGETS)],
    }
