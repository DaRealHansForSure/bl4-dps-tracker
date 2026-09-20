"""The readout's flash bookkeeping and number parsing, with the SDK stubbed out.

readout.py imports the SDK at module level, so the stubs go into sys.modules before the
import; the package itself is imported first, while no stub exists, so its guarded SDK
import stays inactive.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

import dmgtracker  # noqa: F401 - must come before the stubs, see the module docstring

sys.modules.setdefault("unrealsdk", SimpleNamespace(make_struct=lambda name, **fields: (name, fields), unreal=SimpleNamespace(UObject=object)))
sys.modules.setdefault("unrealsdk.unreal", SimpleNamespace(UObject=object))
sys.modules.setdefault("mods_base", SimpleNamespace(ObjectFlags=SimpleNamespace(KEEP_ALIVE=0x80)))

from dmgtracker.readout import FLASH_PEAK_ALPHA, FLASH_S, Readout, ReadoutLayout, _number_in  # noqa: E402
from dmgtracker.table import STYLE_ACTIVE, STYLE_ROW, STYLE_TOTAL, TableRow  # noqa: E402

LAYOUT = ReadoutLayout(x=0, y=0, font_size=20, column_gap=10, column_widths=(100, 80, 80, 80), background_alpha=0.5)


class FakeBackground:
    def __init__(self) -> None:
        self.alphas: list[float] = []

    def SetBrushColor(self, color) -> None:  # noqa: N802 - Unreal spelling
        self.alphas.append(color[1]["A"])


def readout_with_rows(rows: int = 3) -> tuple[Readout, list[FakeBackground]]:
    readout = Readout(LAYOUT)
    readout._widget = object()
    readout.exists = lambda: True  # type: ignore[method-assign]
    backgrounds = [FakeBackground() for _ in range(rows)]
    readout._row_backgrounds = backgrounds  # type: ignore[assignment]
    readout._flash_started = [0.0] * rows
    readout._cell_texts = [["", "", "", ""] for _ in range(rows)]
    readout._row_shown = [True] * rows
    readout._cells = [[] for _ in range(rows)]
    return readout, backgrounds


class NumberTests(unittest.TestCase):
    def test_swiss_numbers_and_text(self) -> None:
        self.assertEqual(_number_in("12'345.5"), 12345.5)
        self.assertEqual(_number_in("0"), 0.0)
        self.assertEqual(_number_in("-"), 0.0)
        self.assertEqual(_number_in(""), 0.0)


class FlashTests(unittest.TestCase):
    def test_a_growing_total_starts_a_flash_that_fades_out(self) -> None:
        readout, backgrounds = readout_with_rows()
        readout._cell_texts[1][1] = "1'000"
        self.assertTrue(readout._total_grew(1, TableRow(("Gun", "1'200", "10", "1.0"), STYLE_ROW)))
        self.assertFalse(readout._total_grew(1, TableRow(("Gun", "900", "10", "1.0"), STYLE_ROW)), "the window forgetting hits is not activity")
        self.assertFalse(readout._total_grew(1, TableRow(("Gun", "1'000", "12", "1.0"), STYLE_ROW)), "a DPS change alone is not activity")
        readout._start_flash(1, now=10.0)
        self.assertEqual(backgrounds[1].alphas, [FLASH_PEAK_ALPHA])
        self.assertTrue(readout._flashing)
        readout.fade(10.0 + FLASH_S / 2)
        self.assertAlmostEqual(backgrounds[1].alphas[-1], FLASH_PEAK_ALPHA / 2)
        self.assertTrue(readout._flashing)
        readout.fade(10.0 + FLASH_S + 0.1)
        self.assertEqual(backgrounds[1].alphas[-1], 0.0)
        self.assertFalse(readout._flashing)
        self.assertEqual(backgrounds[0].alphas, [], "other rows are never touched")
        readout.fade(20.0)
        self.assertEqual(len(backgrounds[1].alphas), 3, "no flash, no calls")

    def test_hiding_a_row_ends_its_flash(self) -> None:
        readout, backgrounds = readout_with_rows()
        readout._start_flash(2, now=5.0)
        readout._hide_row(2)
        self.assertEqual(backgrounds[2].alphas[-1], 0.0)
        self.assertEqual(readout._flash_started[2], 0.0)

    def test_only_weapon_and_source_rows_flash(self) -> None:
        readout, backgrounds = readout_with_rows()
        readout._cell_texts[0][1] = "5"
        self.assertTrue(readout._total_grew(0, TableRow(("Skill", "50", "1", "1.0"), STYLE_ACTIVE)))
        # The total row grows with every hit; the render path only flashes FLASH_STYLES rows.
        from dmgtracker.readout import FLASH_STYLES
        self.assertNotIn(STYLE_TOTAL, FLASH_STYLES)


if __name__ == "__main__":
    unittest.main()
