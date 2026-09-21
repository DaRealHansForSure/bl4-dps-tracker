"""
Lifetime per-weapon record, persisted as JSON next to the session logs.

Answers "which of my guns performs best" across sessions: damage, hits, crits, kills and
seconds in hand per weapon instance, from which DPS in hand, average hit and crit rate
follow, plus the peak DPS the live table ever showed for the gun: its damage over the
window's fighting time, over a window of at least PEAK_MIN_WINDOW_SECONDS, so one lucky
second is not a peak. The non-weapon sources (skill, grenade, melee) get the same peak.
Files of schema 2 held peaks of the DPS in hand, a different quantity; loading one keeps
the records and drops those peaks.

Records are keyed by the item id from itemidentity.py (the seed bytes of the item, stable
across launches). Files written by schema 1 were keyed by the instance id, which shifted
between launches; such a file is set aside on load and the store starts empty.

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

from .attribution import GRENADE_SOURCE, MELEE_SOURCE, SKILL_SOURCE
from .formatting import format_number, format_share
from .itemidentity import short_item_id

STORE_SCHEMA = 3
# Schema 2 records are the same apart from their peaks, which measured the DPS in hand.
FIRST_SCHEMA_WITH_WINDOW_PEAKS = 3
OLDEST_READABLE_SCHEMA = 2
DEFAULT_SAVE_INTERVAL_S = 30.0
MIN_HELD_SECONDS_FOR_DPS = 1.0
PEAK_MIN_WINDOW_SECONDS = 10.0
# The non-weapon sources whose row gets a lifetime peak; "other" is unattributed damage.
PEAK_SOURCE_KINDS = (SKILL_SOURCE.kind, GRENADE_SOURCE.kind, MELEE_SOURCE.kind)
SOURCE_WEAPON = "weapon"
KIND_HEAL = "heal"
KIND_DOT = "dot"
UNKNOWN_LABEL = "Unknown"


@dataclass
class WeaponRecord:
    item_id: str
    label: str
    body: str
    # The item name as the inventory shows it ("Maintained Screeching T.K's Wave"), read
    # from the game's item cards (itemnames.py); empty until it was read once.
    name: str = ""
    damage: float = 0.0
    hits: int = 0
    # Hits that are not status ticks; only those can be crits, so the crit rate is over them.
    direct_hits: int = 0
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
        if event.get("kind") != KIND_DOT:
            self.direct_hits += 1
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

    def crit_rate(self) -> float:
        """Crits per direct hit, 0 without any."""
        if self.direct_hits <= 0:
            return 0.0
        return self.crits / self.direct_hits

    def crit_share(self) -> str:
        return format_share(self.crits, self.direct_hits)

    def display_label(self) -> str:
        """The item name when it is known, the manufacturer-and-type label otherwise."""
        return self.name or self.label

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> WeaponRecord:
        return cls(
            item_id=str(data["item_id"]),
            label=str(data.get("label", UNKNOWN_LABEL)),
            body=str(data.get("body", "")),
            name=str(data.get("name", "")),
            damage=float(data.get("damage", 0.0)),
            hits=int(data.get("hits", 0)),
            # Records written before direct hits were counted: treat every hit as direct.
            direct_hits=int(data.get("direct_hits", data.get("hits", 0))),
            crits=int(data.get("crits", 0)),
            kills=int(data.get("kills", 0)),
            held_seconds=float(data.get("held_seconds", 0.0)),
            first_seen=float(data.get("first_seen", 0.0)),
            last_seen=float(data.get("last_seen", 0.0)),
            peak_dps=float(data.get("peak_dps", 0.0)),
            peak_dps_at=float(data.get("peak_dps_at", 0.0)),
        )


@dataclass
class SourcePeak:
    """The peak DPS of one non-weapon source kind, as its table row showed it."""

    kind: str
    peak_dps: float = 0.0
    peak_dps_at: float = 0.0

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SourcePeak:
        return cls(
            kind=str(data["kind"]),
            peak_dps=float(data.get("peak_dps", 0.0)),
            peak_dps_at=float(data.get("peak_dps_at", 0.0)),
        )


class WeaponStore:
    """All weapon records and the source peaks, loaded from and saved to one JSON file."""

    def __init__(self, path: Path, save_interval_s: float = DEFAULT_SAVE_INTERVAL_S) -> None:
        self.path = path
        self.save_interval_s = save_interval_s
        self.records: dict[str, WeaponRecord] = {}
        self.source_peaks_by_kind: dict[str, SourcePeak] = {}
        self.load_error = ""
        self.set_aside_old_schema: Path | None = None
        self.peaks_dropped = 0
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
            schema = int(data.get("schema", 1))
            if schema < OLDEST_READABLE_SCHEMA:
                # Keyed by the unstable instance id: the records cannot be matched to guns.
                self.set_aside_old_schema = self._set_aside(f"schema{schema}")
                return
            records = [WeaponRecord.from_json(item) for item in data["weapons"]]
            source_peaks = [SourcePeak.from_json(item) for item in data.get("sources", [])]
            if schema < FIRST_SCHEMA_WITH_WINDOW_PEAKS:
                self.peaks_dropped = self._drop_peaks(records)
        except (OSError, ValueError, KeyError, TypeError) as ex:
            self.load_error = f"{type(ex).__name__}: {ex}"
            self._set_aside_corrupt_file()
            return
        with self._lock:
            self.records = {record.item_id: record for record in records}
            self.source_peaks_by_kind = {peak.kind: peak for peak in source_peaks}
            if self.peaks_dropped:
                self._dirty = True

    @staticmethod
    def _drop_peaks(records: list[WeaponRecord]) -> int:
        """Forget peaks measured as DPS in hand; the window DPS peaks start from zero."""
        dropped = 0
        for record in records:
            if record.peak_dps > 0:
                record.peak_dps = 0.0
                record.peak_dps_at = 0.0
                dropped += 1
        return dropped

    def save_if_due(self, now: float, force: bool = False) -> bool:
        """Write the file when something changed and the save interval has passed (or when
        forced). Safe to call from a background thread."""
        with self._lock:
            due = force or (now - self._last_save >= self.save_interval_s)
            if not self._dirty or not due:
                return False
            snapshot = [asdict(record) for record in self.records.values()]
            source_snapshot = [asdict(peak) for peak in self.source_peaks_by_kind.values()]
            self._dirty = False
            self._last_save = now
        payload = {"schema": STORE_SCHEMA, "saved": now, "weapons": snapshot, "sources": source_snapshot}
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
            self.source_peaks_by_kind = {}
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
        item_id = str(event.get("wid", ""))
        if item_id == "":
            return
        with self._lock:
            record = self._record(item_id, str(event.get("weapon", UNKNOWN_LABEL)), str(event.get("body", "")), float(event["t"]))
            record.add(event)
            self._dirty = True

    def add_held(self, item_id: str, label: str, body: str, seconds: float, now: float) -> None:
        if item_id == "" or seconds <= 0:
            return
        with self._lock:
            record = self._record(item_id, label, body, now)
            record.held_seconds += seconds
            self._dirty = True

    def note_peak(self, item_id: str, label: str, body: str, dps: float, window_seconds: float, now: float) -> None:
        """Offer a weapon row's current DPS (its window damage over the window's fighting
        time) as a peak candidate."""
        if item_id == "" or window_seconds < PEAK_MIN_WINDOW_SECONDS:
            return
        with self._lock:
            record = self._record(item_id, label, body, now)
            if record.note_dps(dps, now):
                self._dirty = True

    def note_source_peak(self, kind: str, dps: float, window_seconds: float, now: float) -> None:
        """Offer a non-weapon row's current DPS (its window damage over the window's fighting
        time) as a peak candidate."""
        if kind not in PEAK_SOURCE_KINDS or window_seconds < PEAK_MIN_WINDOW_SECONDS:
            return
        with self._lock:
            peak = self.source_peaks_by_kind.get(kind)
            if peak is None:
                peak = SourcePeak(kind)
                self.source_peaks_by_kind[kind] = peak
            if dps <= peak.peak_dps:
                return
            peak.peak_dps = dps
            peak.peak_dps_at = now
            self._dirty = True

    def set_name(self, item_id: str, name: str, label: str, body: str, now: float) -> bool:
        """Remember the item name of a weapon; True when it is new or changed."""
        with self._lock:
            record = self._record(item_id, label, body, now)
            if record.name == name:
                return False
            record.name = name
            self._dirty = True
            return True

    def name_for(self, item_id: str) -> str:
        """The stored item name of a weapon, '' when none was read yet."""
        record = self.records.get(item_id)
        return record.name if record is not None else ""

    def _record(self, item_id: str, label: str, body: str, now: float) -> WeaponRecord:
        record = self.records.get(item_id)
        if record is None:
            record = WeaponRecord(item_id, label, body, first_seen=now)
            self.records[item_id] = record
        # A label read too early is "Unknown"; take the real one as soon as it shows up.
        if record.label == UNKNOWN_LABEL and label != UNKNOWN_LABEL:
            record.label = label
            record.body = body
        record.last_seen = now
        return record

    # ------------------------------------------------------------------ reading

    def peaks(self) -> dict[str, float]:
        """Peak DPS in hand per weapon item id, for the table's Best DPS column."""
        with self._lock:
            return {item_id: record.peak_dps for item_id, record in self.records.items() if record.peak_dps > 0}

    def source_peaks(self) -> dict[str, float]:
        """Peak DPS per non-weapon source kind, for the table's Best DPS column."""
        with self._lock:
            return {kind: peak.peak_dps for kind, peak in self.source_peaks_by_kind.items() if peak.peak_dps > 0}

    def format_source_peaks(self) -> list[str]:
        """One console line per non-weapon source with a peak, in the table's order."""
        peaks = self.source_peaks()
        labels = {SKILL_SOURCE.kind: SKILL_SOURCE.label, GRENADE_SOURCE.kind: GRENADE_SOURCE.label, MELEE_SOURCE.kind: MELEE_SOURCE.label}
        lines: list[str] = []
        for kind in PEAK_SOURCE_KINDS:
            if kind in peaks:
                lines.append(f"    {labels[kind]:24} peak DPS {format_number(peaks[kind]):>12}")
        return lines

    def ranking(self, by: str = "dps") -> list[WeaponRecord]:
        """Records best first: by DPS in hand (default), by peak DPS, or by damage."""
        with self._lock:
            records = list(self.records.values())
        if by == "peak":
            return sorted(records, key=lambda record: (record.peak_dps, record.damage), reverse=True)
        if by == "damage":
            return sorted(records, key=lambda record: (record.damage, record.dps_in_hand()), reverse=True)
        return sorted(records, key=lambda record: (record.dps_in_hand(), record.damage), reverse=True)

    def format_ranking(self, limit: int = 15, by: str = "dps") -> list[str]:
        """Text lines for the console: one header, one line per weapon."""
        lines = [
            f"{'#':>2}  {'Weapon':30} {'DPS in hand':>12} {'Peak DPS':>10} {'Damage':>14} {'Hits':>6} "
            f"{'Avg hit':>9} {'Crit %':>7} {'Kills':>6} {'In hand':>9}"
        ]
        for position, record in enumerate(self.ranking(by)[:limit], start=1):
            minutes = record.held_seconds / 60.0
            lines.append(
                f"{position:>2}  {record.display_label()[:22] + ' ' + short_item_id(record.item_id):30} "
                f"{format_number(record.dps_in_hand()):>12} {format_number(record.peak_dps):>10} "
                f"{format_number(record.damage):>14} {record.hits:>6} {format_number(record.average_hit()):>9} "
                f"{record.crit_share():>7} {record.kills:>6} {format_number(minutes, decimals=1) + ' min':>9}"
            )
        if len(lines) == 1:
            lines.append("no weapon data yet")
        return lines
