"""The dashboard server: HTTP page and API, WebSocket feed, lifecycle. Runs on a free port."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from dmgtracker.dashboard import Dashboard, DashboardSources, lan_address
from dmgtracker.table import TableRow
from dmgtracker.weaponstats import WeaponStore
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from tests.test_webdata import ROGUE, hit, write_session

RECV_TIMEOUT_S = 5.0


def fetch(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def fetch_json(url: str) -> tuple[int, dict]:
    status, _headers, body = fetch(url)
    return status, json.loads(body.decode("utf-8"))


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp.name)
        self.session = self.log_dir / "session-20260920-120000.jsonl"
        write_session(self.session, 2000.0, [(ROGUE, [hit(2001.0, 100.0), hit(2002.0, 50.0, weapon="Vladof Assault Rifle", wid="seed280")])])
        self.store = WeaponStore(self.log_dir / "weapons.json")
        self.store.add_event(hit(1.0, 500.0))
        self.store.add_held("seed277", "Jakobs Sniper", "", 20.0, 2.0)
        self.messages: list[str] = []
        sources = DashboardSources(
            version="1.2.3",
            log_dir=self.log_dir,
            weapon_store=lambda: self.store,
            character=lambda: ROGUE,
            session_path=lambda: self.session,
            window_minutes=lambda: 5.0,
            encounter_gap_s=lambda: 15.0,
        )
        self.dashboard = Dashboard(0, sources, self.messages.append, host="127.0.0.1")
        self.assertTrue(self.dashboard.start(), self.dashboard.start_error)
        self.url = self.dashboard.url

    def tearDown(self) -> None:
        self.dashboard.stop()
        self.temp.cleanup()

    def test_page_and_status(self) -> None:
        status, headers, body = fetch(self.url)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"BL4 Damage Tracker", body)
        status, payload = fetch_json(self.url + "api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], "1.2.3")
        self.assertEqual(payload["character"], ROGUE)
        self.assertEqual(payload["session"], self.session.name)
        self.assertEqual(payload["weapons"], 1)
        self.assertEqual(payload["clients"], 0)

    def test_api_reads_the_files_and_the_store(self) -> None:
        status, sessions = fetch_json(self.url + "api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(sessions["current"], self.session.name)
        self.assertEqual(sessions["sessions"][0]["characters"], ["RoboDealer"])
        status, hits = fetch_json(self.url + "api/hits?page=1&size=1")
        self.assertEqual((status, hits["total"], hits["pages"]), (200, 2, 2))
        self.assertEqual(hits["events"][0]["dmg"], 50.0)
        status, hits = fetch_json(self.url + f"api/hits?session={self.session.name}&q=jakobs")
        self.assertEqual((status, hits["total"]), (200, 1))
        status, weapons = fetch_json(self.url + "api/weapons?by=peak")
        self.assertEqual((status, weapons["by"], weapons["top"][0]["label"]), (200, "peak", "Jakobs Sniper"))
        status, encounters = fetch_json(self.url + "api/encounters")
        self.assertEqual((status, len(encounters["encounters"])), (200, 1))
        status, session_weapons = fetch_json(self.url + "api/session-weapons")
        self.assertEqual((status, len(session_weapons["weapons"])), (200, 2))

    def test_unknown_paths_and_sessions(self) -> None:
        status, _headers, _body = fetch(self.url + "nothing/here")
        self.assertEqual(status, 404)
        status, payload = fetch_json(self.url + "api/hits?session=session-19990101-000000.jsonl")
        self.assertEqual((status, payload["error"]), (404, "no such session"))
        status, payload = fetch_json(self.url + "api/hits?session=..%2Fweapons.json")
        self.assertEqual(status, 404)
        self.assertEqual(self.dashboard.errors, 0)

    def test_websocket_feed(self) -> None:
        with connect(self.url.replace("http://", "ws://") + "ws") as client:
            hello = json.loads(client.recv(RECV_TIMEOUT_S))
            self.assertEqual(hello["type"], "hello")
            self.assertEqual((hello["version"], hello["character"], hello["window_minutes"], hello["rows"]), ("1.2.3", ROGUE, 5.0, []))
            self.assertEqual([event["dmg"] for event in hello["recent"]], [50.0, 100.0], "the session's newest hits, newest first")
            rows = [TableRow(("Weapon", "Total"), "header"), TableRow(("Jakobs Sniper", "1'000"), "active")]
            self.dashboard.publish_rows(rows)
            self.dashboard.publish_rows(rows)
            table = json.loads(client.recv(RECV_TIMEOUT_S))
            self.assertEqual(table["type"], "table")
            self.assertEqual(table["rows"], [{"cells": ["Weapon", "Total"], "style": "header"}, {"cells": ["Jakobs Sniper", "1'000"], "style": "active"}])
            self.dashboard.publish_lines([{"t": 5.0, "held": "seed277", "weapon": "Jakobs Sniper", "s": 1.0}, hit(6.0, 42.0)])
            hits = json.loads(client.recv(RECV_TIMEOUT_S))
            self.assertEqual((hits["type"], len(hits["events"]), hits["events"][0]["dmg"]), ("hits", 1, 42.0))
            self.dashboard.publish_character({"key": "x", "class": "Char_DarkSiren", "name": "DarkSiren"})
            character = json.loads(client.recv(RECV_TIMEOUT_S))
            self.assertEqual((character["type"], character["character"]["name"]), ("character", "DarkSiren"))
            self.assertEqual(self.dashboard.client_count(), 1)
            with connect(self.url.replace("http://", "ws://") + "ws") as second:
                second_hello = json.loads(second.recv(RECV_TIMEOUT_S))
                self.assertEqual(second_hello["rows"][1]["cells"][0], "Jakobs Sniper", "a new client gets the latest table at once")

    def test_foreign_origin_is_refused(self) -> None:
        with self.assertRaises(InvalidStatus) as refused:
            with connect(self.url.replace("http://", "ws://") + "ws", additional_headers={"Origin": "http://evil.example"}):
                pass
        self.assertEqual(refused.exception.response.status_code, 403)
        with connect(self.url.replace("http://", "ws://") + "ws", additional_headers={"Origin": self.url.rstrip("/")}) as client:
            self.assertEqual(json.loads(client.recv(RECV_TIMEOUT_S))["type"], "hello")

    def test_stop_ends_the_server(self) -> None:
        self.dashboard.stop()
        self.assertFalse(self.dashboard.running())
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            fetch(self.url)
        self.dashboard.stop()

    def test_port_in_use_is_reported_not_raised(self) -> None:
        other = Dashboard(self.dashboard.port, self.dashboard.sources, self.messages.append, host="127.0.0.1")
        self.assertFalse(other.start())
        self.assertIn("Error", other.start_error)

    def test_lan_address_is_a_string(self) -> None:
        self.assertIsInstance(lan_address(), str)


if __name__ == "__main__":
    unittest.main()
