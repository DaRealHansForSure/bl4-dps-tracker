"""
The one stats engine: encounters and a rolling window, fed one event at a time.

The on-screen readout feeds it live, report.py replays a session file through it, and a
future dashboard can tail a file into it, so all of them agree on what an encounter is.

An encounter opens on the first damage event and closes after ENCOUNTER_GAP_S seconds with
no damage. Healing events are ignored. Damage over time counts in the encounter total but
is held out of the per-source split, because a burn tick lands seconds after the weapon
that applied it was swapped away.

No SDK imports here, and nothing above Python 3.11 syntax: the dev machine runs the tests
on 3.11 while the game embeds 3.14.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

ENCOUNTER_GAP_S = 5.0
ROLLING_WINDOW_S = 60.0
# An encounter that is one hit long has no duration; treat anything shorter as one second
# so DPS stays finite and a single big hit reads as "that much per second".
MIN_DURATION_S = 1.0

KIND_DOT = "dot"
KIND_HEAL = "heal"

SourceKey = tuple[str, str, int]


def source_key(event: dict[str, Any]) -> SourceKey:
    """(src, label, instance id): what the per-source split groups by."""
    return (str(event.get("src", "unknown")), str(event.get("weapon", "Unknown")), int(event.get("wid", 0)))


def target_family(target_name: str) -> str:
    """'Char_CatAdult_2147476409' -> 'Char_CatAdult'; names without a numeric suffix stay."""
    stem, separator, suffix = target_name.rpartition("_")
    if separator and suffix.isdigit():
        return stem
    return target_name


@dataclass
class SourceTotals:
    """Damage dealt by one source (a weapon instance, or the 'other' bucket) in an encounter."""

    kind: str
    label: str
    instance_id: int
    damage: float = 0.0
    hits: int = 0
    crits: int = 0
    kills: int = 0

    def add(self, event: dict[str, Any]) -> None:
        self.damage += float(event["dmg"])
        self.hits += 1
        if event.get("crit"):
            self.crits += 1
        if event.get("kill"):
            self.kills += 1


@dataclass
class Encounter:
    """Everything the readout and the report show for one fight."""

    started: float
    last_hit: float
    total: float = 0.0
    hits: int = 0
    crits: int = 0
    kills: int = 0
    dot_damage: float = 0.0
    by_source: dict[SourceKey, SourceTotals] = field(default_factory=dict)
    targets: Counter[str] = field(default_factory=Counter)
    active_seconds: set[int] = field(default_factory=set)

    def add(self, event: dict[str, Any]) -> None:
        damage = float(event["dmg"])
        self.last_hit = float(event["t"])
        self.total += damage
        self.hits += 1
        if event.get("crit"):
            self.crits += 1
        if event.get("kill"):
            self.kills += 1
        self.active_seconds.add(int(event["t"]))
        target = str(event.get("target") or "")
        if target:
            self.targets[target_family(target)] += 1
        if event.get("kind") == KIND_DOT:
            self.dot_damage += damage
            return
        key = source_key(event)
        totals = self.by_source.get(key)
        if totals is None:
            totals = SourceTotals(key[0], key[1], key[2])
            self.by_source[key] = totals
        totals.add(event)

    def duration(self) -> float:
        """Wall-clock seconds from the first to the last hit, at least MIN_DURATION_S."""
        return max(self.last_hit - self.started, MIN_DURATION_S)

    def dps(self) -> float:
        """Honest average: total over the whole duration, downtime and reloads included."""
        return self.total / self.duration()

    def source_dps(self, totals: SourceTotals) -> float:
        return totals.damage / self.duration()

    def active_dps(self) -> float:
        """Total over only the seconds in which at least one hit landed."""
        active = max(len(self.active_seconds), 1)
        return self.total / active

    def other_damage(self) -> float:
        return sum(totals.damage for totals in self.by_source.values() if totals.kind != "weapon")

    def top_targets(self, limit: int = 3) -> list[tuple[str, int]]:
        return self.targets.most_common(limit)


class EncounterTracker:
    """Groups events into encounters using the gap rule. Feed events in time order.

    max_closed bounds the history kept in memory; the live readout only ever shows the
    latest encounter and sessions run for hours, while the report replays a file and
    needs every encounter, so it leaves the bound off.
    """

    def __init__(self, gap_s: float = ENCOUNTER_GAP_S, max_closed: int | None = None) -> None:
        self.gap_s = gap_s
        self.max_closed = max_closed
        self.current: Encounter | None = None
        self.closed: list[Encounter] = []
        self.closed_total = 0

    def add(self, event: dict[str, Any]) -> None:
        if event.get("kind") == KIND_HEAL:
            return
        timestamp = float(event["t"])
        if self.current is not None and timestamp - self.current.last_hit > self.gap_s:
            self._close()
        if self.current is None:
            self.current = Encounter(started=timestamp, last_hit=timestamp)
        self.current.add(event)

    def close_if_idle(self, now: float) -> bool:
        """Close the open encounter when the gap has passed without a hit. The live readout
        calls this every tick so a fight ends on screen without waiting for the next hit."""
        if self.current is None or now - self.current.last_hit <= self.gap_s:
            return False
        self._close()
        return True

    def finish(self) -> None:
        """Close whatever is open; used when a whole file has been replayed."""
        if self.current is not None:
            self._close()

    def latest(self) -> Encounter | None:
        """The open encounter, or the most recently closed one, or None before any hit."""
        if self.current is not None:
            return self.current
        if self.closed:
            return self.closed[-1]
        return None

    def all_encounters(self) -> list[Encounter]:
        encounters = list(self.closed)
        if self.current is not None:
            encounters.append(self.current)
        return encounters

    def _close(self) -> None:
        if self.current is None:
            return
        self.closed.append(self.current)
        self.closed_total += 1
        self.current = None
        if self.max_closed is not None and len(self.closed) > self.max_closed:
            del self.closed[: len(self.closed) - self.max_closed]


class RollingWindow:
    """Sum of damage over the last window_s seconds, kept in O(1) per event."""

    def __init__(self, window_s: float = ROLLING_WINDOW_S) -> None:
        self.window_s = window_s
        self._hits: deque[tuple[float, float]] = deque()
        self._sum = 0.0

    def add(self, timestamp: float, damage: float) -> None:
        self._hits.append((timestamp, damage))
        self._sum += damage

    def total_at(self, now: float) -> float:
        """Damage in the window ending at now; drops what fell out of it."""
        cutoff = now - self.window_s
        while self._hits and self._hits[0][0] < cutoff:
            _, damage = self._hits.popleft()
            self._sum -= damage
        if not self._hits:
            self._sum = 0.0
        return self._sum

    def dps_at(self, now: float) -> float:
        return self.total_at(now) / self.window_s
