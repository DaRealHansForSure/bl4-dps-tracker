"""
Number formatting for everything the user reads: the on-screen table, report output, docs.

Swiss convention: apostrophe as the thousands separator, dot as the decimal point, so
1234567.5 renders as 1'234'567.5. Logs keep raw floats; this is display formatting only.
"""

from __future__ import annotations

THOUSANDS_SEPARATOR = "'"


def format_number(value: float, decimals: int = 0) -> str:
    """Format a number the Swiss way: 1'234'567 or 1'234'567.5 with the given decimals."""
    # Python's "," grouping is exactly the grouping we want; only the separator differs.
    return f"{value:,.{decimals}f}".replace(",", THOUSANDS_SEPARATOR)


def format_share(part: float, whole: float) -> str:
    """Format part as a percentage of whole with one decimal, "0.0" when whole is zero."""
    if whole <= 0:
        return format_number(0.0, decimals=1)
    return format_number(100.0 * part / whole, decimals=1)


def format_duration(seconds: float) -> str:
    """Format a duration as m:ss, e.g. 2:07 for 127 seconds."""
    whole_seconds = int(round(seconds))
    minutes, remainder = divmod(whole_seconds, 60)
    return f"{minutes}:{remainder:02d}"


def parse_number(text: str) -> float:
    """The value of a number as format_number wrote it; 0 for anything that is not one."""
    try:
        return float(text.replace(THOUSANDS_SEPARATOR, ""))
    except ValueError:
        return 0.0
