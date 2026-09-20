"""
Phase 0 probe #11: render a per-weapon damage table on screen with runtime UMG.

probe_ui.py proved a single TextBlock works. A table needs real columns (the default font
is proportional, so padding with spaces would never line up), right-aligned numbers, a
smaller font and a translucent background. This probe builds:

    Border (dark translucent background)
      GridPanel
        header row + one row per weapon + a total row, TextBlock per cell

    pyexec probe_table.py     show a sample table near the top left
    pyexec probe_ui_off.py    remove it (same builtins slot as probe_ui.py)

Every step is guarded so partial success is visible in the output.
"""

from __future__ import annotations

import builtins
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
CONTAINER_CLASSES = ("GbxUIUMGUserWidget", "GbxUIUMGListViewItemWidget")
Z_ORDER_ABOVE_HUD = 10000
SCREEN_POSITION = (120.0, 320.0)
FONT_SIZE = 30
FONT_PATH = "/Engine/EngineFonts/Roboto.Roboto"
TEXT_JUSTIFY_RIGHT = 2
COLUMN_GAP = 44.0
ROW_GAP = 2.0
SPACER_HEIGHT_FONT = 10
COLUMN_MIN_WIDTHS = (320.0, 240.0, 190.0, 140.0)

COLOR_HEADER = (0.75, 0.75, 0.75, 1.0)
COLOR_ROW = (1.0, 1.0, 1.0, 1.0)
COLOR_ACTIVE = (1.0, 0.85, 0.3, 1.0)
COLOR_TOTAL = (0.6, 0.85, 1.0, 1.0)
COLOR_SHADOW = (0.0, 0.0, 0.0, 0.8)

# (cells, colour, is_spacer)
SAMPLE_ROWS = [
    (("Weapon", "Total", "DPS", "Share [%]"), COLOR_HEADER, False),
    (("Vladof AR", "1'234'567", "51'687", "62.1"), COLOR_ACTIVE, False),
    (("Borg SMG", "456'789", "19'032", "23.0"), COLOR_ROW, False),
    (("Jakobs Sniper", "296'010", "12'334", "14.9"), COLOR_ROW, False),
    (("Torgue Shotgun", "0", "0", "0.0"), COLOR_ROW, False),
    (("", "", "", ""), COLOR_ROW, True),
    (("Encounter", "1'987'366", "82'807", "100.0"), COLOR_TOTAL, False),
]


def say(msg: str) -> None:
    logging.info(f"[dmgtable] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def described(fn) -> str:
    """Run fn and return its result as a string, or the error text instead of raising."""
    try:
        value = fn()
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"
    if isinstance(value, UObject):
        return f"{value._path_name()} ({value.Class.Name})"
    return repr(value)[:300]


def attempt(label: str, fn) -> Any:
    """Run one optional styling step; report only failures so the log stays readable."""
    try:
        return fn()
    except Exception as ex:  # noqa: BLE001
        say(f"   {label}: {type(ex).__name__}: {ex}")
        return None


def create_container(pc: UObject) -> UObject:
    library = unrealsdk.find_class("WidgetBlueprintLibrary").ClassDefaultObject
    for class_name in CONTAINER_CLASSES:
        container_class = unrealsdk.find_class(class_name)
        widget = library.Create(pc, container_class, pc)
        if widget is not None:
            say(f"   container: {class_name}")
            return widget
        say(f"   {class_name}: Create returned None")
    raise RuntimeError("no container class worked")


def make_font_info(size: int) -> Any:
    font = unrealsdk.find_object("Font", FONT_PATH)
    return unrealsdk.make_struct("SlateFontInfo", FontObject=font, Size=size, TypefaceFontName="Regular")


def make_linear_color(rgba: tuple[float, float, float, float]) -> Any:
    return unrealsdk.make_struct("LinearColor", R=rgba[0], G=rgba[1], B=rgba[2], A=rgba[3])


def make_slate_color(rgba: tuple[float, float, float, float]) -> Any:
    # ColorUseRule 0 is UseColor_Specified.
    return unrealsdk.make_struct("SlateColor", SpecifiedColor=make_linear_color(rgba), ColorUseRule=0)


def make_cell(tree: UObject, text: str, index: int, column: int, font_info: Any, color: tuple) -> UObject:
    cell = unrealsdk.construct_object("TextBlock", tree, f"Cell{index}")
    cell.SetText(text)
    if font_info is not None:
        attempt(f"cell {index} SetFont", lambda: cell.SetFont(font_info))
    if column > 0:
        attempt(f"cell {index} SetJustification", lambda: cell.SetJustification(TEXT_JUSTIFY_RIGHT))
    attempt(f"cell {index} SetMinDesiredWidth", lambda: cell.SetMinDesiredWidth(COLUMN_MIN_WIDTHS[column]))
    attempt(f"cell {index} SetColorAndOpacity", lambda: cell.SetColorAndOpacity(make_slate_color(color)))
    attempt(f"cell {index} SetShadowOffset", lambda: cell.SetShadowOffset(
        unrealsdk.make_struct("Vector2D", X=1.5, Y=1.5)))
    attempt(f"cell {index} SetShadowColorAndOpacity", lambda: cell.SetShadowColorAndOpacity(
        make_linear_color(COLOR_SHADOW)))
    return cell


def build_table(widget: UObject) -> None:
    tree = widget.WidgetTree
    border = unrealsdk.construct_object("Border", tree, "TableBorder")
    grid = unrealsdk.construct_object("GridPanel", tree, "TableGrid")
    tree.RootWidget = border
    attempt("Border.SetContent", lambda: border.SetContent(grid))
    attempt("Border.SetBrushColor", lambda: border.SetBrushColor(make_linear_color((0.0, 0.0, 0.0, 0.6))))
    attempt("Border.SetPadding", lambda: border.SetPadding(
        unrealsdk.make_struct("Margin", Left=14.0, Top=8.0, Right=14.0, Bottom=8.0)))

    font_info = attempt("make SlateFontInfo", lambda: make_font_info(FONT_SIZE))
    spacer_font = attempt("make spacer SlateFontInfo", lambda: make_font_info(SPACER_HEIGHT_FONT))
    cell_index = 0
    for row_index, (cells, color, is_spacer) in enumerate(SAMPLE_ROWS):
        row_font = spacer_font if is_spacer else font_info
        for column_index, text in enumerate(cells):
            cell = make_cell(tree, text, cell_index, column_index, row_font, color)
            cell_index += 1
            slot = grid.AddChildToGrid(cell, row_index, column_index)
            gap_left = 0.0 if column_index == 0 else COLUMN_GAP
            attempt(f"slot {row_index},{column_index} SetPadding", lambda: slot.SetPadding(
                unrealsdk.make_struct("Margin", Left=gap_left, Top=ROW_GAP, Right=0.0, Bottom=ROW_GAP)))
    say(f"   {cell_index} cells added")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} table probe")
pc = get_pc()

old_widget = getattr(builtins, "_dmgui_widget", None)
if old_widget is not None:
    say(f"   removing widget from an earlier run: {described(lambda: old_widget.RemoveFromParent())}")
    builtins._dmgui_widget = None  # type: ignore[attr-defined]

try:
    widget = create_container(pc)
    build_table(widget)
    builtins._dmgui_widget = widget  # type: ignore[attr-defined]
    widget.AddToViewport(Z_ORDER_ABOVE_HUD)
    position = unrealsdk.make_struct("Vector2D", X=SCREEN_POSITION[0], Y=SCREEN_POSITION[1])
    widget.SetPositionInViewport(position, True)
    say("   table added; check alignment, font size and background, then `pyexec probe_ui_off.py`")
except Exception as ex:  # noqa: BLE001
    say(f"   failed: {type(ex).__name__}: {ex}")
