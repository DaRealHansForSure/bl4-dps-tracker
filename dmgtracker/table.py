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
from .stats import DamageRate, Encounter, SourceKey, SourceTotals, kind_totals, scope_totals

STYLE_HEADER = "header"
STYLE_ROW = "row"
STYLE_ACTIVE = "active"
STYLE_TOTAL = "total"
STYLE_SPACER = "spacer"

HEADER_CELLS = ("Weapon", "Total", "DPS")
SHARE_HEADER_CELL = "Share [%]"
CRIT_AND_KILLS_HEADER_CELLS = ("Crit [%]", "Kills")
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

    def other_damage(self) -> float: ...


@dataclass(frozen=True)
class TableRow:
    cells: tuple[str, ...]
    style: str


@dataclass(frozen=True)
class PeakDps:
    """Lifetime peaks for the Best DPS column: per weapon item id, and per non-weapon
    source kind ("skill", "grenade", ...)."""

    weapons: dict[str, float]
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
    held_item_id: str,
    encounter: Encounter | None = None,
    best: PeakDps | None = None,
    hidden_kinds: frozenset[str] = frozenset(),
    crit_and_kills: bool = False,
    share: bool = True,
    fight: DamageRate | None = None,
) -> list[TableRow]:
    """The complete table for the current state. The row count only depends on the settings
    (encounter row, hidden non-weapon rows), never on the data. Optional columns, in this
    order after Weapon, Total and DPS: Share [%] (`share`), Crit [%] and Kills
    (`crit_and_kills`), then Best DPS (`best`, the lifetime peak DPS of each weapon and
    non-weapon source). With `fight`, the DPS column is the open fight's rate (zero between
    fights); without it, the window average."""
    columns = TableColumns(share, crit_and_kills, fight)
    header = HEADER_CELLS
    if share:
        header += (SHARE_HEADER_CELL,)
    if crit_and_kills:
        header += CRIT_AND_KILLS_HEADER_CELLS
    if best is not None:
        header += (BEST_HEADER_CELL,)
    count = len(header)
    rows = [TableRow(header, STYLE_HEADER)]
    rows.extend(_weapon_slot_rows(window, equipped, held_item_id, best, columns))
    rows.extend(_non_weapon_rows(window, hidden_kinds, best, columns))
    rows.append(TableRow(("",) * count, STYLE_SPACER))
    rows.append(_total_row(window_label(window_minutes), window, columns))
    if encounter is not None:
        rows.append(_encounter_row(encounter, columns))
    return [_padded(row, count) for row in rows]


@dataclass(frozen=True)
class TableColumns:
    """Which optional value columns a row carries, and the fight the DPS column rates
    (None: the window average)."""

    share: bool
    crit_and_kills: bool
    fight: DamageRate | None = None

    def dps(self, key: SourceKey | None, window_damage: float, window_duration: float) -> float:
        """The DPS cell of a row: the fight's rate of its source (None: of everything), or
        the window average when no fight was given."""
        if self.fight is not None:
            return self.fight.rate(key)
        return window_damage / window_duration


def _padded(row: TableRow, columns: int) -> TableRow:
    if len(row.cells) == columns:
        return row
    cells = tuple(row.cells[:columns]) + ("",) * max(columns - len(row.cells), 0)
    return TableRow(cells, row.style)


def _weapon_slot_rows(
    scope: DamageScope, equipped: list[DamageSource], held_item_id: str, best: PeakDps | None, columns: TableColumns
) -> list[TableRow]:
    rows: list[TableRow] = []
    duration = scope.duration()
    for slot in range(WEAPON_SLOT_ROWS):
        if slot >= len(equipped):
            empty = SourceTotals(SOURCE_WEAPON, EMPTY_SLOT_LABEL, "")
            rows.append(TableRow(_source_cells(EMPTY_SLOT_LABEL, empty, 0.0, scope.total, columns), STYLE_ROW))
            continue
        weapon = equipped[slot]
        totals = _totals_for(scope, weapon)
        style = STYLE_ACTIVE if weapon.item_id == held_item_id else STYLE_ROW
        # The DPS of a gun is its rate in the open fight (the lifetime record keeps the DPS
        # in hand for comparing guns across fights).
        dps = columns.dps((SOURCE_WEAPON, weapon.label, weapon.item_id), totals.damage, duration)
        row = TableRow(_source_cells(weapon.label, totals, dps, scope.total, columns), style)
        if best is not None:
            row = _with_best_cell(row, best.weapons.get(weapon.item_id, 0.0))
        rows.append(row)
    return rows


def _non_weapon_rows(
    scope: DamageScope, hidden_kinds: frozenset[str], best: PeakDps | None, columns: TableColumns
) -> list[TableRow]:
    rows: list[TableRow] = []
    duration = scope.duration()
    for source in NON_WEAPON_SOURCES:
        if source.kind in hidden_kinds:
            continue
        totals = kind_totals(scope.by_source, source.kind)
        dps = _kind_dps(scope, source.kind, totals.damage, duration, columns)
        row = TableRow(_source_cells(source.label, totals, dps, scope.total, columns), STYLE_ROW)
        if best is not None:
            row = _with_best_cell(row, best.sources.get(source.kind, 0.0))
        rows.append(row)
    return rows


def _kind_dps(scope: DamageScope, kind: str, window_damage: float, duration: float, columns: TableColumns) -> float:
    """A non-weapon row gathers every source of its kind (melee from several labels, say),
    so its fight rate is the sum over those sources."""
    if columns.fight is None:
        return window_damage / duration
    return columns.fight.kind_rate(kind)


def _source_cells(label: str, totals: SourceTotals, dps: float, scope_total: float, columns: TableColumns) -> tuple[str, ...]:
    """Label, Total, DPS, then the share, the crit rate and the kills when those columns are on."""
    cells = [label, format_number(totals.damage), format_number(dps)]
    if columns.share:
        cells.append(format_share(totals.damage, scope_total))
    if columns.crit_and_kills:
        cells.append(format_share(totals.crits, totals.direct_hits))
        cells.append(format_number(totals.kills))
    return tuple(cells)


def _with_best_cell(row: TableRow, peak: float) -> TableRow:
    """Append the Best DPS cell; blank until there is a peak."""
    return TableRow(row.cells + (format_number(peak) if peak > 0 else "",), row.style)


def _totals_for(scope: DamageScope, weapon: DamageSource) -> SourceTotals:
    for totals in scope.by_source.values():
        if totals.kind == SOURCE_WEAPON and totals.item_id == weapon.item_id:
            return totals
    return SourceTotals(weapon.kind, weapon.label, weapon.item_id)


def _total_row(label: str, scope: DamageScope, columns: TableColumns) -> TableRow:
    totals = scope_totals(scope.by_source)
    totals.damage = scope.total
    return TableRow(_source_cells(label, totals, columns.dps(None, scope.total, scope.duration()), scope.total, columns), STYLE_TOTAL)


def _encounter_row(encounter: Encounter, columns: TableColumns) -> TableRow:
    """The fight's own numbers: its DPS is its damage over its duration, whatever the DPS
    column shows elsewhere."""
    totals = scope_totals(encounter.by_source)
    totals.damage = encounter.total
    return TableRow(_source_cells(ENCOUNTER_LABEL, totals, encounter.dps(), encounter.total, columns), STYLE_TOTAL)
