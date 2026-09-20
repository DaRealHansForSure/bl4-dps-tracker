"""
The on-screen table: a runtime UMG widget holding a fixed grid of TextBlocks.

Built the way poc/probe_table.py proved on 2026-09-20: a UserWidget (a concrete native
subclass, since UserWidget itself is abstract) whose root is a translucent Border around a
GridPanel; one TextBlock per cell. All cells are created once, then only their text, colour
and visibility change, and only when a value differs from what is already on screen.

A row whose Total grows gets a flash: a Border behind the row (one per row, spanning every
column, on a lower grid layer) is lit and fades out over FLASH_S; fade() runs every frame
while a flash is active and touches nothing otherwise.

This is the one module besides mod.py that touches the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import unrealsdk
from mods_base import ObjectFlags
from unrealsdk.unreal import UObject

from .formatting import parse_number
from .table import MAX_ROWS, STYLE_ACTIVE, STYLE_HEADER, STYLE_ROW, STYLE_SPACER, STYLE_TOTAL, TableRow

CONTAINER_CLASSES = ("GbxUIUMGUserWidget", "GbxUIUMGListViewItemWidget")
FONT_PATH = "/Engine/EngineFonts/Roboto.Roboto"
TYPEFACE_REGULAR = "Regular"
TYPEFACE_BOLD = "Bold"
Z_ORDER_ABOVE_HUD = 10000
SPACER_FONT_SIZE = 10
ROW_GAP = 2.0
TEXT_JUSTIFY_RIGHT = 2
VISIBILITY_COLLAPSED = 1
VISIBILITY_HIT_TEST_INVISIBLE = 3
ALIGN_FILL = 0
# The row flash: white at this alpha, fading to nothing over FLASH_S after new damage.
FLASH_S = 0.9
FLASH_RGB = (1.0, 1.0, 1.0)
FLASH_PEAK_ALPHA = 0.28
FLASH_STYLES = (STYLE_ROW, STYLE_ACTIVE)
TOTAL_COLUMN = 1
BACKGROUND_LAYER = -1

BACKGROUND_RGB = (0.0, 0.0, 0.0)
SHADOW_COLOR = (0.0, 0.0, 0.0, 0.8)
SHADOW_OFFSET = (1.5, 1.5)
BORDER_PADDING = (14.0, 8.0, 14.0, 8.0)
STYLE_COLORS = {
    STYLE_HEADER: (0.75, 0.75, 0.75, 1.0),
    STYLE_ROW: (1.0, 1.0, 1.0, 1.0),
    STYLE_ACTIVE: (1.0, 0.85, 0.3, 1.0),
    STYLE_TOTAL: (0.6, 0.85, 1.0, 1.0),
    STYLE_SPACER: (1.0, 1.0, 1.0, 1.0),
}


@dataclass(frozen=True)
class ReadoutLayout:
    """Position and sizes, all in UMG units (scaled by the game's UI scale)."""

    x: float
    y: float
    font_size: int
    column_gap: float
    column_widths: tuple[float, ...]

    @property
    def columns(self) -> int:
        return len(self.column_widths)
    background_alpha: float


class Readout:
    """Owns the widget. create() and destroy() bracket its life; render() updates the cells."""

    def __init__(self, layout: ReadoutLayout) -> None:
        self.layout = layout
        self._widget: UObject | None = None
        self._cells: list[list[UObject]] = []
        self._cell_texts: list[list[str]] = []
        self._row_styles: list[str] = []
        self._row_shown: list[bool] = []
        self._row_backgrounds: list[UObject] = []
        self._flash_started: list[float] = []
        self._flashing = False
        self._visible = True
        self._widget_path = ""
        self._widget_class_name = ""
        self._fonts: dict[tuple[int, str], Any] = {}
        self._colors: dict[str, Any] = {}

    def is_created(self) -> bool:
        return self._widget is not None

    def visible(self) -> bool:
        """True while the table is on screen (not hidden for a menu)."""
        return self._widget is not None and self._visible

    def exists(self) -> bool:
        """Whether the widget object still exists in the game. A world teardown destroys it
        behind our back; the lookup goes by name and never touches the old wrapper."""
        if self._widget is None:
            return False
        try:
            unrealsdk.find_object(self._widget_class_name, self._widget_path)
        except ValueError:
            return False
        return True

    def forget(self) -> None:
        """Drop every reference without calling into the game, for a widget that is gone."""
        self._widget = None
        self._cells = []
        self._cell_texts = []
        self._row_styles = []
        self._row_shown = []
        self._row_backgrounds = []
        self._flash_started = []
        self._flashing = False

    # ------------------------------------------------------------------ lifecycle

    def create(self, player_controller: UObject) -> None:
        """Build the widget and put it on screen. Raises when the UMG calls fail."""
        widget = self._create_container(player_controller)
        # Nothing on the Unreal side references the widget once it leaves the viewport, so
        # mark it root-set; destroy() clears the flag again.
        widget.ObjectFlags |= ObjectFlags.KEEP_ALIVE
        self._build_table(widget)
        widget.AddToViewport(Z_ORDER_ABOVE_HUD)
        widget.SetPositionInViewport(self._vector2d(self.layout.x, self.layout.y), True)
        widget.SetVisibility(VISIBILITY_HIT_TEST_INVISIBLE)
        self._visible = True
        self._widget = widget
        self._widget_path = str(widget._path_name())
        self._widget_class_name = str(widget.Class.Name)

    def destroy(self) -> None:
        widget = self._widget
        alive = self.exists()
        self.forget()
        if widget is None or not alive:
            return
        try:
            # After a world teardown the engine has already pulled the widget off the screen;
            # only detach it ourselves while it is still shown.
            if widget.IsInViewport():
                widget.RemoveFromParent()
        finally:
            widget.ObjectFlags &= ~ObjectFlags.KEEP_ALIVE

    # ------------------------------------------------------------------ updates

    def set_visible(self, visible: bool) -> None:
        if self._widget is None or visible == self._visible:
            return
        if not self.exists():
            self.forget()
            return
        self._visible = visible
        visibility = VISIBILITY_HIT_TEST_INVISIBLE if visible else VISIBILITY_COLLAPSED
        self._widget.SetVisibility(visibility)

    def render(self, rows: list[TableRow], now: float = 0.0) -> None:
        """Show these rows; unused grid rows collapse. Only changed cells are touched. `now`
        (monotonic seconds) stamps the flash of a row whose Total grew."""
        if self._widget is None:
            return
        if not self.exists():
            self.forget()
            return
        for row_index in range(MAX_ROWS):
            if row_index < len(rows):
                self._render_row(row_index, rows[row_index], now)
            else:
                self._hide_row(row_index)

    def fade(self, now: float) -> None:
        """Advance the running flashes; call every frame. Nothing happens without a flash."""
        if not self._flashing or self._widget is None:
            return
        if not self.exists():
            self.forget()
            return
        still_flashing = False
        for row_index, started in enumerate(self._flash_started):
            if started <= 0.0:
                continue
            progress = (now - started) / FLASH_S
            if progress >= 1.0:
                self._flash_started[row_index] = 0.0
                alpha = 0.0
            else:
                still_flashing = True
                alpha = FLASH_PEAK_ALPHA * (1.0 - progress)
            self._row_backgrounds[row_index].SetBrushColor(self._linear_color((*FLASH_RGB, alpha)))
        self._flashing = still_flashing

    def _render_row(self, row_index: int, row: TableRow, now: float) -> None:
        cells = self._cells[row_index]
        if row.style in FLASH_STYLES and self._total_grew(row_index, row):
            self._start_flash(row_index, now)
        style_changed = self._row_styles[row_index] != row.style
        if style_changed:
            self._row_styles[row_index] = row.style
            color = self._color(row.style)
            size = SPACER_FONT_SIZE if row.style == STYLE_SPACER else self.layout.font_size
            typeface = TYPEFACE_BOLD if row.style == STYLE_HEADER else TYPEFACE_REGULAR
            font = self._font(size, typeface)
            for cell in cells:
                cell.SetColorAndOpacity(color)
                cell.SetFont(font)
        for column, text in enumerate(row.cells[: len(cells)]):
            if self._cell_texts[row_index][column] != text:
                self._cell_texts[row_index][column] = text
                cells[column].SetText(text)
        if not self._row_shown[row_index]:
            self._row_shown[row_index] = True
            for cell in cells:
                cell.SetVisibility(VISIBILITY_HIT_TEST_INVISIBLE)

    def _hide_row(self, row_index: int) -> None:
        if not self._row_shown[row_index]:
            return
        self._row_shown[row_index] = False
        for cell in self._cells[row_index]:
            cell.SetVisibility(VISIBILITY_COLLAPSED)
        if self._flash_started[row_index] > 0.0:
            self._flash_started[row_index] = 0.0
            self._row_backgrounds[row_index].SetBrushColor(self._linear_color((*FLASH_RGB, 0.0)))

    def _total_grew(self, row_index: int, row: TableRow) -> bool:
        """Whether the row's Total is higher than what is on screen: new damage, not the
        window forgetting old hits (which lowers it) and not a mere refresh."""
        if len(row.cells) <= TOTAL_COLUMN:
            return False
        return parse_number(row.cells[TOTAL_COLUMN]) > parse_number(self._cell_texts[row_index][TOTAL_COLUMN])

    def _start_flash(self, row_index: int, now: float) -> None:
        if now <= 0.0:
            return
        self._flash_started[row_index] = now
        self._flashing = True
        self._row_backgrounds[row_index].SetBrushColor(self._linear_color((*FLASH_RGB, FLASH_PEAK_ALPHA)))

    # ------------------------------------------------------------------ construction

    def _create_container(self, player_controller: UObject) -> UObject:
        library = unrealsdk.find_class("WidgetBlueprintLibrary").ClassDefaultObject
        for class_name in CONTAINER_CLASSES:
            widget = library.Create(player_controller, unrealsdk.find_class(class_name), player_controller)
            if widget is not None:
                return widget
        raise RuntimeError(f"none of {CONTAINER_CLASSES} could be created as the widget container")

    def _build_table(self, widget: UObject) -> None:
        tree = widget.WidgetTree
        border = unrealsdk.construct_object("Border", tree, "DmgTrackerBorder")
        grid = unrealsdk.construct_object("GridPanel", tree, "DmgTrackerGrid")
        tree.RootWidget = border
        border.SetContent(grid)
        border.SetBrushColor(self._linear_color((*BACKGROUND_RGB, self.layout.background_alpha)))
        border.SetPadding(self._margin(*BORDER_PADDING))

        normal_font = self._font(self.layout.font_size)
        row_color = self._color(STYLE_ROW)
        shadow_color = self._linear_color(SHADOW_COLOR)
        shadow_offset = self._vector2d(*SHADOW_OFFSET)
        for row_index in range(MAX_ROWS):
            self._add_row_background(tree, grid, row_index)
            row_cells: list[UObject] = []
            for column in range(self.layout.columns):
                cell = unrealsdk.construct_object("TextBlock", tree, f"DmgTrackerCell_{row_index}_{column}")
                cell.SetFont(normal_font)
                cell.SetColorAndOpacity(row_color)
                cell.SetShadowOffset(shadow_offset)
                cell.SetShadowColorAndOpacity(shadow_color)
                cell.SetMinDesiredWidth(self.layout.column_widths[column])
                if column > 0:
                    cell.SetJustification(TEXT_JUSTIFY_RIGHT)
                cell.SetVisibility(VISIBILITY_COLLAPSED)
                slot = grid.AddChildToGrid(cell, row_index, column)
                gap_left = 0.0 if column == 0 else self.layout.column_gap
                slot.SetPadding(self._margin(gap_left, ROW_GAP, 0.0, ROW_GAP))
                row_cells.append(cell)
            self._cells.append(row_cells)
            self._cell_texts.append([""] * self.layout.columns)
            self._row_styles.append(STYLE_ROW)
            self._row_shown.append(False)
            self._flash_started.append(0.0)

    def _add_row_background(self, tree: UObject, grid: UObject, row_index: int) -> None:
        """A transparent Border across the whole row, painted below the cells (added first, and
        on a lower layer where the slot supports it), lit by the flash."""
        background = unrealsdk.construct_object("Border", tree, f"DmgTrackerRowBackground_{row_index}")
        background.SetBrushColor(self._linear_color((*FLASH_RGB, 0.0)))
        background.SetPadding(self._margin(0.0, 0.0, 0.0, 0.0))
        background.SetVisibility(VISIBILITY_HIT_TEST_INVISIBLE)
        slot = grid.AddChildToGrid(background, row_index, 0)
        slot.SetColumnSpan(self.layout.columns)
        slot.SetHorizontalAlignment(ALIGN_FILL)
        slot.SetVerticalAlignment(ALIGN_FILL)
        try:
            slot.SetLayer(BACKGROUND_LAYER)
        except AttributeError:
            pass  # add order already puts the background behind the cells
        self._row_backgrounds.append(background)

    # ------------------------------------------------------------------ struct helpers

    def _font(self, size: int, typeface: str = TYPEFACE_REGULAR) -> Any:
        font_info = self._fonts.get((size, typeface))
        if font_info is None:
            font_object = unrealsdk.find_object("Font", FONT_PATH)
            font_info = unrealsdk.make_struct("SlateFontInfo", FontObject=font_object, Size=size, TypefaceFontName=typeface)
            self._fonts[(size, typeface)] = font_info
        return font_info

    def _color(self, style: str) -> Any:
        slate_color = self._colors.get(style)
        if slate_color is None:
            # ColorUseRule 0 is UseColor_Specified.
            slate_color = unrealsdk.make_struct(
                "SlateColor", SpecifiedColor=self._linear_color(STYLE_COLORS[style]), ColorUseRule=0
            )
            self._colors[style] = slate_color
        return slate_color

    @staticmethod
    def _linear_color(rgba: tuple[float, float, float, float]) -> Any:
        return unrealsdk.make_struct("LinearColor", R=rgba[0], G=rgba[1], B=rgba[2], A=rgba[3])

    @staticmethod
    def _margin(left: float, top: float, right: float, bottom: float) -> Any:
        return unrealsdk.make_struct("Margin", Left=left, Top=top, Right=right, Bottom=bottom)

    @staticmethod
    def _vector2d(x: float, y: float) -> Any:
        return unrealsdk.make_struct("Vector2D", X=x, Y=y)

