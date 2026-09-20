"""Swiss number formatting: apostrophe thousands separator, dot decimal point."""

from __future__ import annotations

import unittest

from dmgtracker.formatting import format_duration, format_number, format_share


class FormatNumberTests(unittest.TestCase):
    def test_small_numbers_have_no_separator(self) -> None:
        self.assertEqual(format_number(0), "0")
        self.assertEqual(format_number(999), "999")

    def test_thousands_use_apostrophes(self) -> None:
        self.assertEqual(format_number(1000), "1'000")
        self.assertEqual(format_number(1234567), "1'234'567")

    def test_decimals_use_a_dot(self) -> None:
        self.assertEqual(format_number(1234567.5, decimals=1), "1'234'567.5")
        self.assertEqual(format_number(51687.25, decimals=2), "51'687.25")

    def test_rounds_to_the_requested_decimals(self) -> None:
        self.assertEqual(format_number(2.5), "2")
        self.assertEqual(format_number(3.5), "4")
        self.assertEqual(format_number(0.96, decimals=1), "1.0")

    def test_negative_numbers(self) -> None:
        self.assertEqual(format_number(-1234.5, decimals=1), "-1'234.5")


class FormatShareTests(unittest.TestCase):
    def test_share_has_one_decimal(self) -> None:
        self.assertEqual(format_share(621, 1000), "62.1")
        self.assertEqual(format_share(1000, 1000), "100.0")

    def test_zero_whole_gives_zero_share(self) -> None:
        self.assertEqual(format_share(0, 0), "0.0")
        self.assertEqual(format_share(5, 0), "0.0")


class FormatDurationTests(unittest.TestCase):
    def test_minutes_and_padded_seconds(self) -> None:
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(7.4), "0:07")
        self.assertEqual(format_duration(127), "2:07")
        self.assertEqual(format_duration(3600), "60:00")


if __name__ == "__main__":
    unittest.main()
