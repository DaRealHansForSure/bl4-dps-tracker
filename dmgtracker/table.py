"""
The on-screen table as data: which rows, which text in which cell, which style.

Kept apart from the rendering so the layout can be tested without the game. readout.py
turns these rows into UMG cells.

The layout is fixed: every row has a permanent position, nothing appears, disappears or
reorders while playing (a user rule). Four weapon slot rows in slot order, then the four
non-weapon sources in a fixed order, a spacer, the window total row, and the optional
encounter row. The table is a rolling view: every number covers the last X minutes of
fighting. The encounter row is meant for boss fights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .attribution import (
    GRENADE_SOURCE,
    MELEE_SOURCE,
    OTHER_SOURCE,
    SKILL_SOURCE,
    SOURCE_WEAPON,
    DamageSource,
)
from .formatting import format_number, format_share
from .stats import Encounter, SourceKey, SourceTotals

STYLE_HEADER = "header"
STYLE_ROW = "row"
STYLE_ACTIVE = "active"
STYLE_TOTAL = "total"
STYLE_SPACER = "spacer"

HEADER_CELLS = ("Weapon", "Total", "DPS", "Share [%]")
EMPTY_CELLS = ("", "", "", "")
EMPTY_SLOT_LABEL = "-"
ENCOUNTER_LABEL = "Encounter"

WEAPON_SLOT_ROWS = 4
# Fixed order of the non-weapon rows, always shown.
NON_WEAPON_SOURCES = (SKILL_SOURCE, GRENADE_SOURCE, MELEE_SOURCE, OTHER_SOURCE)
MAX_SOURCE_ROWS = WEAPON_SLOT_ROWS + len(NON_WEAPON_SOURCES)
# header + source rows + spacer + window row + encounter row
MAX_ROWS = 1 + MAX_SOURCE_ROWS + 1 + 1 + 1


class DamageScope(Protocol):
    """What the table needs from a time span: RollingStats and Encounter both provide it."""

    total: float
    dot_damage: float
    by_source: dict[SourceKey, SourceTotals]

    def duration(self) -> float: ...

    def dps(self) -> float: ...

    def source_dps(self, totals: SourceTotals) -> float: ...

    def other_damage(self) -> float: ...


@dataclass(frozen=True)
class TableRow:
    cells: tuple[str, str, str, str]
    style: str


def window_label(window_minutes: float) -> str:
    """'Last 5 min', 'Last 90 s' below one minute, 'Last 2.5 min' for fractions."""
    if window_minutes < 1.0:
        return f"Last {format_number(window_minutes * 60.0)} s"
    if window_minutes == int(window_minutes):
        return f"Last {int(window_minutes)} min"
    return f"Last {format_number(window_minutes, decimals=1)} min"


def build_rows(
    window: DamageScope,
    window_minutes: float,
    equipped: list[DamageSource],
    held_instance_id: int,
    encounter: Encounter | None = None,
) -> list[TableRow]:
    """The complete table for the current state; the row count only depends on whether an
    encounter row is requested."""
    rows = [TableRow(HEADER_CELLS, STYLE_HEADER)]
    rows.extend(_weapon_slot_rows(window, equipped, held_instance_id))
    rows.extend(_non_weapon_rows(window))
    rows.append(TableRow(EMPTY_CELLS, STYLE_SPACER))
    rows.append(_total_row(window_label(window_minutes), window))
    if encounter is not None:
        rows.append(_total_row(ENCOUNTER_LABEL, encounter))
    return rows


def _weapon_slot_rows(scope: DamageScope, equipped: list[DamageSource], held_instance_id: int) -> list[TableRow]:
    rows: list[TableRow] = []
    for slot in range(WEAPON_SLOT_ROWS):
        if slot >= len(equipped):
            rows.append(TableRow((EMPTY_SLOT_LABEL, "0", "0", format_share(0.0, 0.0)), STYLE_ROW))
            continue
        weapon = equipped[slot]
        totals = _totals_for(scope, weapon)
        style = STYLE_ACTIVE if weapon.instance_id == held_instance_id else STYLE_ROW
        rows.append(_weapon_row(weapon.label, totals, scope, style))
    return rows


def _non_weapon_rows(scope: DamageScope) -> list[TableRow]:
    rows: list[TableRow] = []
    duration = scope.duration()
    for source in NON_WEAPON_SOURCES:
        damage = sum(totals.damage for totals in scope.by_source.values() if totals.kind == source.kind)
        rows.append(_damage_row(source.label, damage, duration, scope.total, STYLE_ROW))
    return rows


def _totals_for(scope: DamageScope, weapon: DamageSource) -> SourceTotals:
    for totals in scope.by_source.values():
        if totals.kind == SOURCE_WEAPON and totals.instance_id == weapon.instance_id:
            return totals
    return SourceTotals(weapon.kind, weapon.label, weapon.instance_id)


def _weapon_row(label: str, totals: SourceTotals, scope: DamageScope, style: str) -> TableRow:
    """A weapon's DPS is measured over the time it was in hand, so guns compare fairly."""
    cells = (
        label,
        format_number(totals.damage),
        format_number(scope.source_dps(totals)),
        format_share(totals.damage, scope.total),
    )
    return TableRow(cells, style)


def _damage_row(label: str, damage: float, duration: float, total: float, style: str) -> TableRow:
    cells = (label, format_number(damage), format_number(damage / duration), format_share(damage, total))
    return TableRow(cells, style)


def _total_row(label: str, scope: DamageScope) -> TableRow:
    cells = (label, format_number(scope.total), format_number(scope.dps()), format_share(scope.total, scope.total))
    return TableRow(cells, STYLE_TOTAL)
