"""
The on-screen table as data: which rows, which text in which cell, which style.

Kept apart from the rendering so the layout can be tested without the game. readout.py
turns these rows into UMG cells; the number of rows is capped so the widget can allocate
its cells once and hide the unused ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from .attribution import SOURCE_WEAPON, DamageSource
from .formatting import format_number, format_share
from .stats import Encounter, SourceTotals

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
ROLLING_LABEL = "Last 60 s"

MAX_SOURCE_ROWS = 8
# header + source rows + spacer + encounter + rolling
MAX_ROWS = 1 + MAX_SOURCE_ROWS + 1 + 1 + 1


@dataclass(frozen=True)
class TableRow:
    cells: tuple[str, str, str, str]
    style: str


def build_rows(
    encounter: Encounter | None,
    equipped: list[DamageSource],
    held_instance_id: int,
    rolling_dps: float,
    show_rolling_row: bool,
) -> list[TableRow]:
    """The complete table for the current state, at most MAX_ROWS rows.

    Every equipped weapon gets a row (even at zero damage), in slot order, followed by
    sources that dealt damage in the encounter but are not equipped any more, then the
    Other and DoT rows when they have damage.
    """
    rows = [TableRow(HEADER_CELLS, STYLE_HEADER)]
    rows.extend(_source_rows(encounter, equipped, held_instance_id))
    rows.append(TableRow(EMPTY_CELLS, STYLE_SPACER))
    rows.append(_encounter_row(encounter))
    if show_rolling_row:
        rows.append(TableRow((ROLLING_LABEL, "", format_number(rolling_dps), ""), STYLE_TOTAL))
    return rows


def _source_rows(encounter: Encounter | None, equipped: list[DamageSource], held_instance_id: int) -> list[TableRow]:
    total = encounter.total if encounter is not None else 0.0
    duration = encounter.duration() if encounter is not None else 1.0
    listed_instance_ids: set[int] = set()
    rows: list[TableRow] = []

    for weapon in equipped:
        listed_instance_ids.add(weapon.instance_id)
        totals = _totals_for(encounter, weapon)
        style = STYLE_ACTIVE if weapon.instance_id == held_instance_id else STYLE_ROW
        rows.append(_damage_row(weapon.label, totals.damage, duration, total, style))

    if encounter is not None:
        for totals in encounter.by_source.values():
            if totals.kind != SOURCE_WEAPON or totals.instance_id in listed_instance_ids:
                continue
            listed_instance_ids.add(totals.instance_id)
            rows.append(_damage_row(totals.label, totals.damage, duration, total, STYLE_ROW))
        other_damage = encounter.other_damage()
        if other_damage > 0:
            rows.append(_damage_row(OTHER_LABEL, other_damage, duration, total, STYLE_ROW))
        if encounter.dot_damage > 0:
            rows.append(_damage_row(DOT_LABEL, encounter.dot_damage, duration, total, STYLE_ROW))

    return rows[:MAX_SOURCE_ROWS]


def _totals_for(encounter: Encounter | None, weapon: DamageSource) -> SourceTotals:
    if encounter is not None:
        for totals in encounter.by_source.values():
            if totals.kind == SOURCE_WEAPON and totals.instance_id == weapon.instance_id:
                return totals
    return SourceTotals(weapon.kind, weapon.label, weapon.instance_id)


def _damage_row(label: str, damage: float, duration: float, total: float, style: str) -> TableRow:
    cells = (label, format_number(damage), format_number(damage / duration), format_share(damage, total))
    return TableRow(cells, style)


def _encounter_row(encounter: Encounter | None) -> TableRow:
    if encounter is None:
        return TableRow((ENCOUNTER_LABEL, "0", "0", "0.0"), STYLE_TOTAL)
    cells = (
        ENCOUNTER_LABEL,
        format_number(encounter.total),
        format_number(encounter.dps()),
        format_share(encounter.total, encounter.total),
    )
    return TableRow(cells, STYLE_TOTAL)
