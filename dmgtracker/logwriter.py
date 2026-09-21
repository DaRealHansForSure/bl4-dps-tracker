"""
Session log: an in-memory buffer that a background thread appends to a JSONL file.

The game thread only ever calls append(), which takes a short lock and extends a list. The
writer thread swaps the buffer out under that lock and does all disk work itself, so a slow
disk can never stall a frame. Append-only JSONL means a crash costs at most one flush
interval of data, and the file can be tailed by another process while the game runs.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SESSION_FILE_PREFIX = "session-"
SESSION_FILE_SUFFIX = ".jsonl"
LATEST_POINTER_NAME = "latest-session.txt"
WRITER_JOIN_TIMEOUT_S = 3.0

FlushListener = Callable[[list[dict[str, Any]]], None]


class SessionLog:
    """One JSONL file per game launch, filled from a buffer by a daemon thread."""

    def __init__(
        self,
        log_dir: Path,
        flush_interval_s: float,
        sessions_to_keep: int,
        meta: dict[str, Any],
    ) -> None:
        self.log_dir = log_dir
        self.flush_interval_s = flush_interval_s
        self.sessions_to_keep = sessions_to_keep
        self.meta = meta
        self.path: Path | None = None
        self.written = 0
        self.write_errors = 0
        self.last_error = ""
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._file: Any = None
        self._listeners: list[FlushListener] = []
        self._tick_listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ game thread

    def append(self, events: list[dict[str, Any]]) -> None:
        """Queue events for the next flush. Cheap: one lock, one list extend."""
        with self._lock:
            self._buffer.extend(events)

    def buffered(self) -> int:
        with self._lock:
            return len(self._buffer)

    def add_flush_listener(self, listener: FlushListener) -> None:
        """Register a callable that receives every batch right after it reached the disk.
        Runs on the writer thread; a listener that raises is counted as a write error."""
        self._listeners.append(listener)

    def add_tick_listener(self, listener: Callable[[], None]) -> None:
        """Register a callable that runs on the writer thread once per flush interval,
        whether or not anything was written; for periodic work like publishing state."""
        self._tick_listeners.append(listener)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Create the session file, write the meta line, point latest-session.txt at it,
        prune old sessions and start the writer thread."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._prune_old_sessions()
        self.path = self._unused_session_path()
        self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._file.write(json.dumps({"meta": self.meta}) + "\n")
        self._file.flush()
        (self.log_dir / LATEST_POINTER_NAME).write_text(self.path.name + "\n", encoding="utf-8")
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="dmgtracker-writer", daemon=True)
        self._writer.start()

    def stop(self) -> None:
        """Stop the writer thread, flush what is left and close the file."""
        self._stop.set()
        if self._writer is not None:
            self._writer.join(timeout=WRITER_JOIN_TIMEOUT_S)
            self._writer = None
        self._flush()
        if self._file is not None:
            self._file.close()
            self._file = None

    # ------------------------------------------------------------------ writer thread

    def _writer_loop(self) -> None:
        while not self._stop.wait(self.flush_interval_s):
            self._flush()
            for listener in self._tick_listeners:
                try:
                    listener()
                except Exception as ex:  # noqa: BLE001 - a broken listener must not stop logging
                    self.write_errors += 1
                    self.last_error = f"tick listener {type(ex).__name__}: {ex}"

    def _flush(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch or self._file is None:
            return
        try:
            self._file.write("".join(json.dumps(event, separators=(",", ":")) + "\n" for event in batch))
            self._file.flush()
            self.written += len(batch)
        except (OSError, TypeError, ValueError) as ex:
            self.write_errors += 1
            self.last_error = f"{type(ex).__name__}: {ex}"
            return
        for listener in self._listeners:
            try:
                listener(batch)
            except Exception as ex:  # noqa: BLE001 - a broken listener must not stop logging
                self.write_errors += 1
                self.last_error = f"listener {type(ex).__name__}: {ex}"

    # ------------------------------------------------------------------ files

    def _unused_session_path(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        candidate = self.log_dir / f"{SESSION_FILE_PREFIX}{stamp}{SESSION_FILE_SUFFIX}"
        # Two enables within the same second (menu toggling) must not share a file.
        suffix = 2
        while candidate.exists():
            candidate = self.log_dir / f"{SESSION_FILE_PREFIX}{stamp}-{suffix}{SESSION_FILE_SUFFIX}"
            suffix += 1
        return candidate

    def _prune_old_sessions(self) -> None:
        """Delete the oldest session files so that, with the new one, sessions_to_keep remain.
        File names start with a timestamp, so sorting by name sorts by age."""
        existing = sorted(self.log_dir.glob(f"{SESSION_FILE_PREFIX}*{SESSION_FILE_SUFFIX}"))
        keep_existing = max(self.sessions_to_keep - 1, 0)
        for stale in existing[: max(len(existing) - keep_existing, 0)]:
            try:
                stale.unlink()
            except OSError as ex:
                self.write_errors += 1
                self.last_error = f"prune {type(ex).__name__}: {ex}"
