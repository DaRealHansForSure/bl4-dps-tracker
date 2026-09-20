"""Session log: file creation, meta line, flushing, listeners, pruning."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from dmgtracker.logwriter import LATEST_POINTER_NAME, SessionLog

FAST_FLUSH_S = 0.05
META = {"mod": "dmgtracker", "version": "test", "schema": 1, "started": 0.0}


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class SessionLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._temp.name) / "logs"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_start_writes_meta_line_and_pointer(self) -> None:
        log = SessionLog(self.log_dir, FAST_FLUSH_S, sessions_to_keep=20, meta=META)
        log.start()
        try:
            self.assertTrue(log.path.name.startswith("session-"))
            self.assertEqual(read_lines(log.path), [{"meta": META}])
            pointer = (self.log_dir / LATEST_POINTER_NAME).read_text(encoding="utf-8").strip()
            self.assertEqual(pointer, log.path.name)
        finally:
            log.stop()

    def test_events_reach_the_file_and_the_listeners(self) -> None:
        log = SessionLog(self.log_dir, FAST_FLUSH_S, sessions_to_keep=20, meta=META)
        received: list[list[dict]] = []
        log.add_flush_listener(received.append)
        log.start()
        try:
            log.append([{"t": 1.0, "dmg": 10.0}, {"t": 1.1, "dmg": 20.0}])
            deadline = time.time() + 2.0
            while log.written < 2 and time.time() < deadline:
                time.sleep(FAST_FLUSH_S)
            self.assertEqual(log.written, 2)
            self.assertEqual(log.buffered(), 0)
            self.assertEqual(received, [[{"t": 1.0, "dmg": 10.0}, {"t": 1.1, "dmg": 20.0}]])
        finally:
            log.stop()
        lines = read_lines(log.path)
        self.assertEqual(lines[1:], [{"t": 1.0, "dmg": 10.0}, {"t": 1.1, "dmg": 20.0}])

    def test_tick_listeners_run_every_interval(self) -> None:
        log = SessionLog(self.log_dir, FAST_FLUSH_S, sessions_to_keep=20, meta=META)
        ticks: list[int] = []
        log.add_tick_listener(lambda: ticks.append(1))
        log.start()
        try:
            deadline = time.time() + 2.0
            while len(ticks) < 2 and time.time() < deadline:
                time.sleep(FAST_FLUSH_S)
            self.assertGreaterEqual(len(ticks), 2, "runs without any batch written")
        finally:
            log.stop()

    def test_stop_flushes_what_is_left(self) -> None:
        log = SessionLog(self.log_dir, flush_interval_s=60.0, sessions_to_keep=20, meta=META)
        log.start()
        log.append([{"t": 2.0, "dmg": 5.0}])
        log.stop()
        self.assertEqual(read_lines(log.path)[1:], [{"t": 2.0, "dmg": 5.0}])
        self.assertEqual(log.write_errors, 0)

    def test_a_failing_listener_is_counted_not_raised(self) -> None:
        log = SessionLog(self.log_dir, flush_interval_s=60.0, sessions_to_keep=20, meta=META)

        def broken(batch: list[dict]) -> None:
            raise RuntimeError("boom")

        log.add_flush_listener(broken)
        log.start()
        log.append([{"t": 3.0}])
        log.stop()
        self.assertEqual(log.written, 1)
        self.assertEqual(log.write_errors, 1)
        self.assertIn("boom", log.last_error)

    def test_prunes_to_the_configured_number_of_sessions(self) -> None:
        self.log_dir.mkdir(parents=True)
        for stamp in ("20260101-000001", "20260101-000002", "20260101-000003"):
            (self.log_dir / f"session-{stamp}.jsonl").write_text("{}\n", encoding="utf-8")
        log = SessionLog(self.log_dir, flush_interval_s=60.0, sessions_to_keep=3, meta=META)
        log.start()
        log.stop()
        remaining = sorted(path.name for path in self.log_dir.glob("session-*.jsonl"))
        self.assertEqual(len(remaining), 3)
        self.assertNotIn("session-20260101-000001.jsonl", remaining)
        self.assertIn("session-20260101-000003.jsonl", remaining)
        self.assertIn(log.path.name, remaining)


if __name__ == "__main__":
    unittest.main()
