"""
The on-screen table as data: which rows, which text in which cell, which style.

Kept apart from the rendering so the layout can be tested without the game. readout.py
turns these rows into UMG cells.

The layout is fixed: every row has a permanent position, nothing appears, disappears or
reorders while playing (a user rule). Four weapon slot rows in slot order, then the
non-weapon sources in a fixed order (a setting can leave some of them out, Melee by
default), a spacer, the window total row, and the optional encounter row. The table is a
rolling view: every number covers the last X minutes of fighting. The encounter row is
meant for boss fights.
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
from .stats import Encounter, SourceKey, SourceTotals, kind_damage

STYLE_HEADER = "header"
STYLE_ROW = "row"
STYLE_ACTIVE = "active"
STYLE_TOTAL = "total"
STYLE_SPACER = "spacer"

HEADER_CELLS = ("Weapon", "Total", "DPS", "Share [%]")
BEST_HEADER_CELL = "Best DPS"
EMPTY_SLOT_LABEL = "-"
ENCOUNTER_LABEL = "Encounter"
BASE_COLUMNS = len(HEADER_CELLS)

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
    cells: tuple[str, ...]
    style: str


@dataclass(frozen=True)
class PeakDps:
    """Lifetime peaks for the Best DPS column: per weapon instance id, and per non-weapon
    source kind ("skill", "grenade", ...)."""

    weapons: dict[int, float]
    sources: dict[str, float]


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
    best: PeakDps | None = None,
    hidden_kinds: frozenset[str] = frozenset(),
) -> list[TableRow]:
    """The complete table for the current state. The row count only depends on the settings
    (encounter row, hidden non-weapon rows), never on the data. `best` adds a fifth column
    with the lifetime peak DPS of each weapon and non-weapon source."""
    columns = BASE_COLUMNS + (1 if best is not None else 0)
    header = HEADER_CELLS + ((BEST_HEADER_CELL,) if best is not None else ())
    rows = [TableRow(header, STYLE_HEADER)]
    rows.extend(_weapon_slot_rows(window, equipped, held_instance_id, best))
    rows.extend(_non_weapon_rows(window, hidden_kinds, best))
    rows.append(TableRow(("",) * columns, STYLE_SPACER))
    rows.append(_total_row(window_label(window_minutes), window))
    if encounter is not None:
        rows.append(_total_row(ENCOUNTER_LABEL, encounter))
    return [_padded(row, columns) for row in rows]


def _padded(row: TableRow, columns: int) -> TableRow:
    if len(row.cells) == columns:
        return row
    cells = tuple(row.cells[:columns]) + ("",) * max(columns - len(row.cells), 0)
    return TableRow(cells, row.style)


def _weapon_slot_rows(
    scope: DamageScope, equipped: list[DamageSource], held_instance_id: int, best: PeakDps | None
) -> list[TableRow]:
    rows: list[TableRow] = []
    for slot in range(WEAPON_SLOT_ROWS):
        if slot >= len(equipped):
            rows.append(TableRow((EMPTY_SLOT_LABEL, "0", "0", format_share(0.0, 0.0)), STYLE_ROW))
            continue
        weapon = equipped[slot]
        totals = _totals_for(scope, weapon)
        style = STYLE_ACTIVE if weapon.instance_id == held_instance_id else STYLE_ROW
        row = _weapon_row(weapon.label, totals, scope, style)
        if best is not None:
            row = _with_best_cell(row, best.weapons.get(weapon.instance_id, 0.0))
        rows.append(row)
    return rows


def _non_weapon_rows(scope: DamageScope, hidden_kinds: frozenset[str], best: PeakDps | None) -> list[TableRow]:
    rows: list[TableRow] = []
    duration = scope.duration()
    for source in NON_WEAPON_SOURCES:
        if source.kind in hidden_kinds:
            continue
        damage = kind_damage(scope.by_source, source.kind)
        row = _damage_row(source.label, damage, duration, scope.total, STYLE_ROW)
        if best is not None:
            row = _with_best_cell(row, best.sources.get(source.kind, 0.0))
        rows.append(row)
    return rows


def _with_best_cell(row: TableRow, peak: float) -> TableRow:
    """Append the Best DPS cell; blank until there is a peak."""
    return TableRow(row.cells + (format_number(peak) if peak > 0 else "",), row.style)


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
