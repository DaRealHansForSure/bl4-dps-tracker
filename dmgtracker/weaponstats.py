"""
Lifetime per-weapon record, persisted as JSON next to the session logs.

Answers "which of my guns performs best" across sessions: damage, hits, crits, kills and
seconds in hand per weapon instance, from which DPS in hand, average hit and crit rate
follow, plus the peak DPS in hand the live table ever showed for the gun (with at least
PEAK_MIN_HELD_SECONDS of use inside the window, so one lucky second is not a peak). Weapon
instance ids are stable across game restarts (checked over three restarts on 2026-09-20),
so a record survives sessions and a newly found gun starts a fresh one.

The game thread updates records, a background thread saves them; a lock keeps that safe.
No SDK imports, so the tests and the report can use it.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .formatting import format_number, format_share

STORE_SCHEMA = 1
DEFAULT_SAVE_INTERVAL_S = 30.0
MIN_HELD_SECONDS_FOR_DPS = 1.0
PEAK_MIN_HELD_SECONDS = 10.0
SOURCE_WEAPON = "weapon"
KIND_HEAL = "heal"
UNKNOWN_LABEL = "Unknown"


@dataclass
class WeaponRecord:
    instance_id: int
    label: str
    body: str
    damage: float = 0.0
    hits: int = 0
    crits: int = 0
    kills: int = 0
    held_seconds: float = 0.0
    first_seen: float = 0.0
    last_seen: float = 0.0
    peak_dps: float = 0.0
    peak_dps_at: float = 0.0

    def note_dps(self, dps: float, now: float) -> bool:
        """Remember a new peak DPS in hand; True when it is one."""
        if dps <= self.peak_dps:
            return False
        self.peak_dps = dps
        self.peak_dps_at = now
        return True

    def add(self, event: dict[str, Any]) -> None:
        """Count one damage event of this weapon."""
        self.damage += float(event["dmg"])
        self.hits += 1
        if event.get("crit"):
            self.crits += 1
        if event.get("kill"):
            self.kills += 1

    def dps_in_hand(self) -> float:
        if self.held_seconds < MIN_HELD_SECONDS_FOR_DPS:
            return 0.0
        return self.damage / self.held_seconds

    def average_hit(self) -> float:
        if self.hits == 0:
            return 0.0
        return self.damage / self.hits

    def crit_share(self) -> str:
        return format_share(self.crits, self.hits)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> WeaponRecord:
        return cls(
            instance_id=int(data["instance_id"]),
            label=str(data.get("label", UNKNOWN_LABEL)),
            body=str(data.get("body", "")),
            damage=float(data.get("damage", 0.0)),
            hits=int(data.get("hits", 0)),
            crits=int(data.get("crits", 0)),
            kills=int(data.get("kills", 0)),
            held_seconds=float(data.get("held_seconds", 0.0)),
            first_seen=float(data.get("first_seen", 0.0)),
            last_seen=float(data.get("last_seen", 0.0)),
            peak_dps=float(data.get("peak_dps", 0.0)),
            peak_dps_at=float(data.get("peak_dps_at", 0.0)),
        )


class WeaponStore:
    """All weapon records, loaded from and saved to one JSON file."""

    def __init__(self, path: Path, save_interval_s: float = DEFAULT_SAVE_INTERVAL_S) -> None:
        self.path = path
        self.save_interval_s = save_interval_s
        self.records: dict[int, WeaponRecord] = {}
        self.load_error = ""
        self.save_error = ""
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0

    # ------------------------------------------------------------------ persistence

    def load(self) -> None:
        """Read the file if it exists. A corrupt file is set aside, not overwritten, and the
        store starts empty; load_error says what happened."""
        self.load_error = ""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            records = [WeaponRecord.from_json(item) for item in data["weapons"]]
        except (OSError, ValueError, KeyError, TypeError) as ex:
            self.load_error = f"{type(ex).__name__}: {ex}"
            self._set_aside_corrupt_file()
            return
        with self._lock:
            self.records = {record.instance_id: record for record in records}

    def save_if_due(self, now: float, force: bool = False) -> bool:
        """Write the file when something changed and the save interval has passed (or when
        forced). Safe to call from a background thread."""
        with self._lock:
            due = force or (now - self._last_save >= self.save_interval_s)
            if not self._dirty or not due:
                return False
            snapshot = [asdict(record) for record in self.records.values()]
            self._dirty = False
            self._last_save = now
        payload = {"schema": STORE_SCHEMA, "saved": now, "weapons": snapshot}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as ex:
            self.save_error = f"{type(ex).__name__}: {ex}"
            with self._lock:
                self._dirty = True
            return False
        return True

    def reset(self) -> Path | None:
        """Start over: set the current file aside (never delete history) and empty the
        records. Returns where the old file went, or None if there was none."""
        with self._lock:
            self.records = {}
            self._dirty = True
        return self._set_aside("reset")

    def _set_aside_corrupt_file(self) -> None:
        self._set_aside("corrupt")

    def _set_aside(self, reason: str) -> Path | None:
        if not self.path.exists():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = self.path.with_name(f"{self.path.stem}.{reason}-{stamp}{self.path.suffix}")
        try:
            self.path.replace(destination)
        except OSError:
            return None
        return destination

    # ------------------------------------------------------------------ updates

    def add_event(self, event: dict[str, Any]) -> None:
        if event.get("src") != SOURCE_WEAPON or event.get("kind") == KIND_HEAL:
            return
        instance_id = int(event.get("wid", 0))
        if instance_id == 0:
            return
        with self._lock:
            record = self._record(instance_id, str(event.get("weapon", UNKNOWN_LABEL)), str(event.get("body", "")), float(event["t"]))
            record.add(event)
            self._dirty = True

    def add_held(self, instance_id: int, label: str, body: str, seconds: float, now: float) -> None:
        if instance_id == 0 or seconds <= 0:
            return
        with self._lock:
            record = self._record(instance_id, label, body, now)
            record.held_seconds += seconds
            self._dirty = True

    def note_peak(self, instance_id: int, label: str, body: str, dps: float, held_seconds: float, now: float) -> None:
        """Offer the table's current DPS in hand as a peak candidate."""
        if instance_id == 0 or held_seconds < PEAK_MIN_HELD_SECONDS:
            return
        with self._lock:
            record = self._record(instance_id, label, body, now)
            if record.note_dps(dps, now):
                self._dirty = True

    def _record(self, instance_id: int, label: str, body: str, now: float) -> WeaponRecord:
        record = self.records.get(instance_id)
        if record is None:
            record = WeaponRecord(instance_id, label, body, first_seen=now)
            self.records[instance_id] = record
        # A label read too early is "Unknown"; take the real one as soon as it shows up.
        if record.label == UNKNOWN_LABEL and label != UNKNOWN_LABEL:
            record.label = label
            record.body = body
        record.last_seen = now
        return record

    # ------------------------------------------------------------------ reading

    def ranking(self) -> list[WeaponRecord]:
        """Records by DPS in hand, best first; weapons never held long enough come last."""
        with self._lock:
            records = list(self.records.values())
        return sorted(records, key=lambda record: (record.dps_in_hand(), record.damage), reverse=True)

    def format_ranking(self, limit: int = 15) -> list[str]:
        """Text lines for the console: one header, one line per weapon."""
        lines = [
            f"{'#':>2}  {'Weapon':24} {'DPS in hand':>12} {'Peak DPS':>10} {'Damage':>14} {'Hits':>6} "
            f"{'Avg hit':>9} {'Crit %':>7} {'Kills':>6} {'In hand':>9}"
        ]
        for position, record in enumerate(self.ranking()[:limit], start=1):
            minutes = record.held_seconds / 60.0
            lines.append(
                f"{position:>2}  {record.label[:22] + ' #' + str(record.instance_id):24} "
                f"{format_number(record.dps_in_hand()):>12} {format_number(record.peak_dps):>10} "
                f"{format_number(record.damage):>14} {record.hits:>6} {format_number(record.average_hit()):>9} "
                f"{record.crit_share():>7} {record.kills:>6} {format_number(minutes, decimals=1) + ' min':>9}"
            )
        if len(lines) == 1:
            lines.append("no weapon data yet")
        return lines
