"""
The item names by item id, one table for every character.

A gun's name depends on the gun alone (its seed), not on who carries it, so the names live
in one file next to the character folders, `item-names.json`, and not in the per-character
weapon records: a name read once serves every character, survives the record resets of
`dmgreset --weapons` and `--all`, and is written without a single hit having been dealt.
The file is written on an interval (the log writer's tick, off the game thread), never per
change; `dmgreset --names` empties it.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ITEM_NAMES_FILE = "item-names.json"
STORE_SCHEMA = 1
DEFAULT_SAVE_INTERVAL_S = 30.0


@dataclass
class ItemName:
    """A name as read from the game, with the short label of the gun it belongs to and
    when it was read (wall clock seconds)."""

    name: str
    label: str
    seen: float


class ItemNameStore:
    """The names by item id, loaded from and saved to one JSON file. Safe to use from the
    game thread and the writer thread at once."""

    def __init__(self, path: Path, save_interval_s: float = DEFAULT_SAVE_INTERVAL_S) -> None:
        self.path = path
        self.save_interval_s = save_interval_s
        self.load_error = ""
        self.save_error = ""
        self._items: dict[str, ItemName] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0

    def load(self) -> None:
        """Read the file if it exists. A corrupt file is set aside, not overwritten, and the
        store starts empty; load_error says what happened."""
        self.load_error = ""
        if not self.path.exists():
            return
        try:
            items = _parse_items(self.path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as ex:
            self.load_error = f"{type(ex).__name__}: {ex}"
            self._set_aside_corrupt_file()
            return
        with self._lock:
            self._items = items

    def name_for(self, item_id: str) -> str:
        """The stored item name, '' when none was read yet."""
        item = self._items.get(item_id)
        return item.name if item is not None else ""

    def names(self) -> dict[str, str]:
        """item id -> name, a snapshot for the dashboard and the reports."""
        with self._lock:
            return {item_id: item.name for item_id, item in self._items.items()}

    def set_name(self, item_id: str, name: str, label: str, now: float) -> bool:
        """Remember a name; True when it is new or changed."""
        with self._lock:
            item = self._items.get(item_id)
            if item is not None and item.name == name:
                return False
            self._items[item_id] = ItemName(name, label, now)
            self._dirty = True
            return True

    def clear(self) -> int:
        """Forget every name, so they are read from the game again; returns how many."""
        with self._lock:
            count = len(self._items)
            self._items = {}
            self._dirty = self._dirty or count > 0
        return count

    @property
    def count(self) -> int:
        return len(self._items)

    def save_if_due(self, now: float, force: bool = False) -> bool:
        """Write the file when something changed and the save interval has passed (or when
        forced). Safe to call from a background thread."""
        with self._lock:
            due = force or (now - self._last_save >= self.save_interval_s)
            if not self._dirty or not due:
                return False
            snapshot = {item_id: asdict(item) for item_id, item in self._items.items()}
            self._dirty = False
            self._last_save = now
        payload: dict[str, Any] = {"schema": STORE_SCHEMA, "saved": now, "items": snapshot}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as ex:
            self.save_error = f"{type(ex).__name__}: {ex}"
            with self._lock:
                self._dirty = True
            return False
        return True

    def _set_aside_corrupt_file(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            self.path.replace(self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}"))
        except OSError:
            pass


def read_item_names(path: Path) -> dict[str, str]:
    """item id -> name from the table file; empty when there is none or it cannot be read
    (the offline report, which must not touch the file)."""
    try:
        return {item_id: item.name for item_id, item in _parse_items(path).items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _parse_items(path: Path) -> dict[str, ItemName]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item_id): ItemName(str(entry["name"]), str(entry.get("label", "")), float(entry.get("seen", 0.0)))
        for item_id, entry in dict(data["items"]).items()
    }
