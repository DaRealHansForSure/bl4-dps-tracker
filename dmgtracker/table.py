"""
The on-screen table as data: which rows, which text in which cell, which style.

Kept apart from the rendering so the layout can be tested without the game. readout.py
turns these rows into UMG cells; the number of rows is capped so the widget can allocate
its cells once and hide the unused ones.

The table is a rolling view: every number covers the last X minutes (the window), which
is what matters in the open world. The encounter row is optional and meant for boss
fights, where "how much did I deal in that fight" is the question.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .attribution import SOURCE_WEAPON, DamageSource
from .formatting import format_number, format_share
from .stats import Encounter, SourceKey, SourceTotals

STYLE_HEADER = "header"
STYLE_ROW = "row"
STYLE_ACTIVE = "active"
STYLE_TOTAL = "total"
STYLE_SPACER = "spacer"

HEADER_CELLS = ("Weapon", "Total", "DPS", "Share [%]")
EMPTY_CELLS = ("", "", "", "")
DOT_LABEL = "DoT"
OTHER_LABEL = "Other"
ENCOUNTER_LABEL = "Encounter"

MAX_SOURCE_ROWS = 8
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
    """The complete table for the current state, at most MAX_ROWS rows.

    Every equipped weapon gets a row (even at zero damage), in slot order, followed by
    sources that dealt damage in the window but are not equipped any more, then the
    Other and DoT rows when they have damage. The encounter row appears only when an
    encounter is passed in.
    """
    rows = [TableRow(HEADER_CELLS, STYLE_HEADER)]
    rows.extend(_source_rows(window, equipped, held_instance_id))
    rows.append(TableRow(EMPTY_CELLS, STYLE_SPACER))
    rows.append(_total_row(window_label(window_minutes), window))
    if encounter is not None:
        rows.append(_total_row(ENCOUNTER_LABEL, encounter))
    return rows


def _source_rows(scope: DamageScope, equipped: list[DamageSource], held_instance_id: int) -> list[TableRow]:
    total = scope.total
    duration = scope.duration()
    listed_instance_ids: set[int] = set()
    rows: list[TableRow] = []

    for weapon in equipped:
        listed_instance_ids.add(weapon.instance_id)
        totals = _totals_for(scope, weapon)
        style = STYLE_ACTIVE if weapon.instance_id == held_instance_id else STYLE_ROW
        rows.append(_weapon_row(weapon.label, totals, scope, style))

    for totals in scope.by_source.values():
        if totals.kind != SOURCE_WEAPON or totals.instance_id in listed_instance_ids:
            continue
        listed_instance_ids.add(totals.instance_id)
        rows.append(_weapon_row(totals.label, totals, scope, STYLE_ROW))

    other_damage = scope.other_damage()
    if other_damage > 0:
        rows.append(_damage_row(OTHER_LABEL, other_damage, duration, total, STYLE_ROW))
    if scope.dot_damage > 0:
        rows.append(_damage_row(DOT_LABEL, scope.dot_damage, duration, total, STYLE_ROW))

    return rows[:MAX_SOURCE_ROWS]


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
