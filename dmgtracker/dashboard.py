"""
The local web dashboard: one HTTP + WebSocket server, run by the vendored `websockets`
package. It listens on every interface by default (a setting), so a phone or a second screen
on the same network can show it; the API is read-only. The library's sync server gives every
connection a thread; plain HTTP requests (the page, the JSON API) are answered from
`process_request`, WebSocket upgrades on /ws continue into the connection handler.

Live data goes over the WebSocket: the table rows whenever they change, every batch of hits
as the poller sees them, character changes. Everything else is read from the files when the
page asks for it (webdata.py).

The chart wants "what is happening now" next to the table's window average, so every
sample also carries the damage per second of the last CHART_WINDOWS_S seconds (each of
them, the page picks one) for the total and for each source, computed from the hits kept in
memory.

The game thread only calls the publish_*() methods, which copy data under a lock and wake the
broadcaster; every socket and file operation runs on the dashboard's own daemon threads, so a
browser left open never keeps the game from exiting. Nothing here touches game objects, and
no exception leaves a handler: errors are counted and the server keeps going.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from ._vendor import use_vendored_packages

use_vendored_packages()

from websockets.datastructures import Headers  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.http11 import Request, Response  # noqa: E402
from websockets.sync.server import Server, ServerConnection, serve  # noqa: E402

from .formatting import parse_number  # noqa: E402
from .report import default_session_path  # noqa: E402
from .table import STYLE_ACTIVE, STYLE_ROW, STYLE_TOTAL, WEAPON_SLOT_ROWS, TableRow  # noqa: E402
from .webdata import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    SessionCache,
    encounters_payload,
    hits_page,
    list_sessions,
    session_path,
    session_weapons_payload,
    weapons_payload,
)
from .thumbnails import thumbnail_path  # noqa: E402
from .weaponstats import WeaponStore  # noqa: E402

DEFAULT_HOST = "0.0.0.0"
LOCAL_HOST = "127.0.0.1"
WS_PATH = "/ws"
# Any routable address works for the "which interface faces the network" question; nothing is sent.
PROBE_ADDRESS = ("192.0.2.1", 9)
PAGE_PACKAGE = "dmgtracker.web"
PAGE_FILE = "index.html"
THUMBNAILS_PATH = "/thumbnails/"
# A picture is served only under a name the mod itself writes: a seed id, or the
# per-launch fallback of a session without identities ("instance-206").
THUMBNAIL_NAME = re.compile(r"(?:[0-9a-f]{24}|instance-[0-9]{1,12})\.png")
# A gun's picture never changes once written; a page may keep it for a day.
THUMBNAIL_CACHE_CONTROL = "max-age=86400"
# Longest wait of the broadcaster and of a connection handler before they look at the stop flag.
WAKE_INTERVAL_S = 1.0
RECV_TIMEOUT_S = 1.0
THREAD_JOIN_TIMEOUT_S = 3.0
# Hits waiting for the broadcaster while no client reads them; older ones are dropped.
MAX_PENDING_HITS = 5000
# The newest hits of the session sent to a client on connect, so the feed is not empty after a reload.
RECENT_HITS = 50
# Chart samples (time, DPS, damage, the "now" values), one per second while a table exists,
# kept for a client that connects later: five minutes.
CHART_HISTORY = 300
SAMPLE_INTERVAL_S = 1.0
# The chart's "now": damage per second over the last so many seconds, one value per window
# in every sample; `chart_seconds` (a setting) is the window the page starts with.
CHART_WINDOWS_S = (10.0, 30.0, 60.0, 120.0)
DEFAULT_CHART_SECONDS = 30.0
MAX_RECENT_HITS = 20000
SOURCE_WEAPON = "weapon"
WINDOW_ROW_PREFIX = "Last "
EMPTY_SLOT_LABEL = "-"
TOTAL_CELL = 1
DPS_CELL = 2
SOURCE_STYLES = (STYLE_ROW, STYLE_ACTIVE)

Message = dict[str, Any]


@dataclass
class DashboardSources:
    """Where the server reads current state from. Every callable is cheap and safe to call
    from any thread: they return references to objects with their own locks, or values."""

    version: str
    log_dir: Path
    weapon_store: Callable[[], WeaponStore | None]
    character: Callable[[], dict[str, str] | None]
    session_path: Callable[[], Path | None]
    window_minutes: Callable[[], float]
    encounter_gap_s: Callable[[], float]
    # item id -> item name, for the weapon lists (the live table gets them with its rows).
    item_names: Callable[[], dict[str, str]] = lambda: {}
    # item id -> everything the inventory said about the item (name, label, rarity,
    # element, the card's damage figure, the item code), for the page's hover card.
    item_infos: Callable[[], dict[str, dict[str, Any]]] = lambda: {}
    # The folder of the gun pictures (<item id>.png), served under /thumbnails/.
    thumbnails_dir: Path | None = None
    # ElementalType value -> element name, from the game's enum, for the hit lists.
    element_names: Callable[[], dict[int, str]] = lambda: {}


class Dashboard:
    def __init__(
        self,
        port: int,
        sources: DashboardSources,
        log: Callable[[str], None],
        host: str = DEFAULT_HOST,
        chart_seconds: float = DEFAULT_CHART_SECONDS,
    ) -> None:
        self.host = host
        self.port = port
        self.sources = sources
        self.chart_seconds = _nearest_window(float(chart_seconds))
        self.errors = 0
        self.start_error = ""
        self._log = log
        self._logger = _library_logger(log)
        self._server: Server | None = None
        self._accept_thread: threading.Thread | None = None
        self._broadcast_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._state_lock = threading.Lock()
        self._wake = threading.Condition(self._state_lock)
        self._rows_json: list[Message] = []
        self._last_sample_time = 0.0
        self._paused = False
        self._rows_dirty = False
        self._last_rows: list[TableRow] = []
        self._slot_item_ids: list[str] = []
        self._item_names: dict[str, str] = {}
        self._gadget_ids: dict[str, str] = {}
        self._pending_hits: list[Message] = []
        self._pending_character: dict[str, str] | None = None
        self._history: deque[Message] = deque(maxlen=CHART_HISTORY)
        # (time, damage, key) of recent hits, for the chart's "now"; key is the weapon's item
        # id or the source label, whichever the table rows can be matched by.
        self._recent_hits: deque[tuple[float, float, str]] = deque(maxlen=MAX_RECENT_HITS)
        self._clients: set[ServerConnection] = set()
        self._clients_lock = threading.Lock()
        self._sessions = SessionCache()

    # ------------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        """The address for a browser on this machine."""
        return f"http://{LOCAL_HOST}:{self.port}/"

    def urls(self) -> list[str]:
        """The local address plus, when listening on every interface, the address other
        devices on the network can use."""
        urls = [self.url]
        if self.host == DEFAULT_HOST:
            lan = lan_address()
            if lan:
                urls.append(f"http://{lan}:{self.port}/")
        elif self.host != LOCAL_HOST:
            urls.append(f"http://{self.host}:{self.port}/")
        return urls

    def running(self) -> bool:
        return self._server is not None and not self._stopping.is_set()

    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def start(self) -> bool:
        """Bind the port and start the threads. False (with start_error set) when the port is
        taken or cannot be bound; the mod runs on without the dashboard then."""
        self.start_error = ""
        try:
            server = serve(
                self._handle_connection,
                self.host,
                self.port,
                process_request=self._process_request,
                logger=self._logger,
                server_header=None,
            )
        except OSError as ex:
            self.start_error = f"{type(ex).__name__}: {ex}"
            return False
        self._server = server
        self.port = int(server.socket.getsockname()[1])
        self._stopping.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, args=(server,), name="dmgtracker-web", daemon=True)
        self._broadcast_thread = threading.Thread(target=self._broadcast_loop, name="dmgtracker-web-push", daemon=True)
        self._accept_thread.start()
        self._broadcast_thread.start()
        return True

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._stopping.set()
        with self._wake:
            self._wake.notify_all()
        server.shutdown()
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            _close_quietly(client)
        for thread in (self._accept_thread, self._broadcast_thread):
            if thread is not None:
                thread.join(THREAD_JOIN_TIMEOUT_S)
        self._server = None

    # ------------------------------------------------------------------ publishing (other threads)

    def publish_rows(
        self,
        rows: list[TableRow],
        slot_item_ids: list[str] | None = None,
        item_names: dict[str, str] | None = None,
        gadget_ids: dict[str, str] | None = None,
    ) -> None:
        """The table as the game shows it; only a change wakes the broadcaster.
        `slot_item_ids` names the gun in each weapon slot, so the chart's "now" can be
        matched to the slot rows by item, `item_names` gives the page the item names of
        every named item (the table itself carries the short type labels), and
        `gadget_ids` the item id of the equipped grenade and the like by row label."""
        slot_item_ids = list(slot_item_ids or [])
        item_names = dict(item_names or {})
        gadget_ids = dict(gadget_ids or {})
        unchanged = rows == self._last_rows and slot_item_ids == self._slot_item_ids and item_names == self._item_names and gadget_ids == self._gadget_ids
        if unchanged:
            return
        rows_json = [{"cells": list(row.cells), "style": row.style} for row in rows]
        with self._wake:
            self._last_rows = rows
            self._rows_json = rows_json
            self._slot_item_ids = slot_item_ids
            self._item_names = item_names
            self._gadget_ids = gadget_ids
            self._rows_dirty = True
            self._wake.notify()

    def set_paused(self, paused: bool, pause_seconds: float = 0.0) -> None:
        """The game is paused (a menu, a loading screen): the chart gets no samples and its
        "now" lines do not decay. On the way back the recent hits move past the pause, so
        the seconds they are rated over are seconds of play."""
        with self._wake:
            self._paused = paused
            if not paused and pause_seconds > 0:
                self._recent_hits = deque(((when + pause_seconds, damage, key) for when, damage, key in self._recent_hits), maxlen=MAX_RECENT_HITS)
                self._last_sample_time += pause_seconds

    def publish_hits(self, events: list[Message]) -> None:
        """New hits from the poller: to the clients, and into the chart's "now"."""
        if not events:
            return
        with self._wake:
            self._pending_hits.extend(events)
            if len(self._pending_hits) > MAX_PENDING_HITS:
                del self._pending_hits[: len(self._pending_hits) - MAX_PENDING_HITS]
            for event in events:
                self._recent_hits.append((float(event.get("t", 0.0)), float(event.get("dmg", 0.0)), _hit_key(event)))
            self._wake.notify()

    def _instant_dps(self, now: float) -> dict[str, dict[str, float]]:
        """For every chart window: damage per second over its last seconds, by hit key, plus
        "" for the total. Called with the state lock held."""
        cutoff = now - max(CHART_WINDOWS_S)
        while self._recent_hits and self._recent_hits[0][0] < cutoff:
            self._recent_hits.popleft()
        by_window: dict[str, dict[str, float]] = {}
        for window in CHART_WINDOWS_S:
            totals: dict[str, float] = {"": 0.0}
            since = now - window
            for when, damage, key in self._recent_hits:
                if when < since:
                    continue
                totals[""] += damage
                totals[key] = totals.get(key, 0.0) + damage
            by_window[_window_name(window)] = {key: damage / window for key, damage in totals.items()}
        return by_window

    def publish_character(self, identity: dict[str, str]) -> None:
        """A character switch starts the live numbers from zero; the chart follows, and the
        previous character's table is not sampled any more."""
        with self._wake:
            self._pending_character = dict(identity)
            self._history.clear()
            self._recent_hits.clear()
            self._last_rows = []
            self._wake.notify()

    # ------------------------------------------------------------------ server threads

    def _accept_loop(self, server: Server) -> None:
        """The library's serve_forever() runs connection handlers on non-daemon threads, which
        would hold the game process open at exit while a browser tab is connected. This is the
        same loop with daemon threads; shutdown() closes the socket and ends it."""
        while not self._stopping.is_set():
            try:
                sock, address = server.socket.accept()
            except OSError:
                break
            threading.Thread(target=server.handler, args=(sock, address), name="dmgtracker-web-conn", daemon=True).start()

    def _handle_connection(self, connection: ServerConnection) -> None:
        """One WebSocket client: greet it, then keep the connection open until it goes away or
        the dashboard stops. Whatever the client sends is ignored."""
        with self._clients_lock:
            self._clients.add(connection)
        try:
            connection.send(json.dumps(self._hello()))
            while not self._stopping.is_set():
                try:
                    connection.recv(timeout=RECV_TIMEOUT_S)
                except TimeoutError:
                    continue
        except ConnectionClosed:
            pass
        except Exception as ex:  # noqa: BLE001 - one client must not take the server down
            self._count_error(f"client handler: {type(ex).__name__}: {ex}")
        finally:
            with self._clients_lock:
                self._clients.discard(connection)

    def _broadcast_loop(self) -> None:
        while not self._stopping.is_set():
            with self._wake:
                self._wake.wait(WAKE_INTERVAL_S)
                messages = self._take_pending_messages()
                sample = self._sample_if_due(time.time())
            if sample is not None:
                messages.append({"type": "sample", "sample": sample})
            for message in messages:
                self._broadcast(json.dumps(message))

    def _sample_if_due(self, now: float) -> Message | None:
        """One chart sample per second while there is a table: the table's window values and
        the "now" DPS of the last seconds, which keeps falling after the last hit whether or
        not the table changes. Called with the state lock held."""
        if self._paused or not self._last_rows or now - self._last_sample_time < SAMPLE_INTERVAL_S:
            return None
        self._last_sample_time = now
        sample = _window_sample(self._last_rows, now, self._instant_dps(now), self._slot_item_ids)
        if sample is not None:
            # A fight is on while the last hit is younger than the encounter gap: the page
            # shades those stretches of the chart.
            sample["fight"] = bool(self._recent_hits) and now - self._recent_hits[-1][0] <= self.sources.encounter_gap_s()
            self._history.append(sample)
        return sample

    def _take_pending_messages(self) -> list[Message]:
        """Called with the state lock held: everything published since the last round."""
        messages: list[Message] = []
        if self._pending_character is not None:
            messages.append({"type": "character", "character": self._pending_character})
            self._pending_character = None
        if self._rows_dirty:
            messages.append({
                "type": "table",
                "rows": self._rows_json,
                "t": time.time(),
                "slots": self._slot_item_ids,
                "names": self._item_names,
                "gadgets": self._gadget_ids,
            })
            self._rows_dirty = False
        if self._pending_hits:
            messages.append({"type": "hits", "events": self._pending_hits})
            self._pending_hits = []
        return messages

    def _broadcast(self, text: str) -> None:
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.send(text)
            except Exception:  # noqa: BLE001 - a dead client is dropped, nothing else
                with self._clients_lock:
                    self._clients.discard(client)
                _close_quietly(client)

    def _hello(self) -> Message:
        with self._state_lock:
            rows = self._rows_json
            history = list(self._history)
            slots = list(self._slot_item_ids)
            names = dict(self._item_names)
            gadgets = dict(self._gadget_ids)
        return {
            "type": "hello",
            "version": self.sources.version,
            "character": self.sources.character(),
            "window_minutes": self.sources.window_minutes(),
            "chart_seconds": self.chart_seconds,
            "chart_windows": list(CHART_WINDOWS_S),
            "elements": {str(value): name for value, name in self.sources.element_names().items()},
            "rows": rows,
            "slots": slots,
            "names": names,
            "gadgets": gadgets,
            "recent": self._recent_hits_from_file(),
            "history": history,
        }

    def _recent_hits_from_file(self) -> list[Message]:
        """The newest hits of the current session from its file, newest first; the file is at
        most one flush interval behind the game."""
        try:
            session = self._session_for({})
        except Exception as ex:  # noqa: BLE001 - a missing feed must not refuse the client
            self._count_error(f"recent hits: {type(ex).__name__}: {ex}")
            return []
        if session is None:
            return []
        return hits_page(session, 1, RECENT_HITS, "")["events"]

    # ------------------------------------------------------------------ HTTP

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        """Plain HTTP for everything but the WebSocket path; None lets the upgrade proceed."""
        path, _, query_string = request.path.partition("?")
        if path == WS_PATH:
            if _origin_is_foreign(request):
                # A page from another site must not read the live feed through the browser.
                return _text_response(HTTPStatus.FORBIDDEN, "origin not allowed")
            return None
        try:
            return self._respond(path, urllib.parse.parse_qs(query_string))
        except Exception as ex:  # noqa: BLE001 - reported to the client and counted
            self._count_error(f"{path}: {type(ex).__name__}: {ex}")
            return _text_response(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(ex).__name__}: {ex}")

    def _respond(self, path: str, query: dict[str, list[str]]) -> Response:
        if path in ("/", "/index.html"):
            return _response(HTTPStatus.OK, "text/html; charset=utf-8", _page_bytes())
        if path == "/api/status":
            return _json_response(self._status())
        if path == "/api/sessions":
            return _json_response({"sessions": list_sessions(self.sources.log_dir), "current": self._current_session_name()})
        if path == "/api/weapons":
            by = _first(query, "by", "dps")
            return _json_response(weapons_payload(self.sources.weapon_store(), self.sources.character(), by, self.sources.item_names()))
        session = self._session_for(query)
        if session is None:
            return _json_response({"error": "no such session"}, HTTPStatus.NOT_FOUND)
        if path == "/api/hits":
            page = _int(query, "page", 1)
            size = _int(query, "size", DEFAULT_PAGE_SIZE)
            return _json_response(hits_page(session, page, size, _first(query, "q", "")))
        if path == "/api/session-weapons":
            return _json_response({"session": session.path.name, "weapons": session_weapons_payload(session, self.sources.item_names())})
        if path == "/api/encounters":
            return _json_response(encounters_payload(session, self.sources.encounter_gap_s()))
        if path == "/api/items":
            return _json_response({"items": self._items()})
        if path.startswith(THUMBNAILS_PATH):
            return self._thumbnail(path[len(THUMBNAILS_PATH):])
        return _text_response(HTTPStatus.NOT_FOUND, "not found")

    def _items(self) -> dict[str, dict[str, Any]]:
        """Every named item with what the inventory said about it and whether its picture
        is on disk."""
        folder = self.sources.thumbnails_dir
        items = self.sources.item_infos()
        for item_id, info in items.items():
            info["thumbnail"] = folder is not None and thumbnail_path(folder, item_id).is_file()
        return items

    def _thumbnail(self, name: str) -> Response:
        """A gun's picture by file name; only names of the form <item id>.png reach the disk."""
        folder = self.sources.thumbnails_dir
        if folder is None or not THUMBNAIL_NAME.fullmatch(name):
            return _text_response(HTTPStatus.NOT_FOUND, "not found")
        path = folder / name
        if not path.is_file():
            return _text_response(HTTPStatus.NOT_FOUND, "no picture yet")
        return _response(HTTPStatus.OK, "image/png", path.read_bytes(), cache_control=THUMBNAIL_CACHE_CONTROL)

    def _status(self) -> Message:
        store = self.sources.weapon_store()
        return {
            "version": self.sources.version,
            "character": self.sources.character(),
            "session": self._current_session_name(),
            "window_minutes": self.sources.window_minutes(),
            "encounter_gap_s": self.sources.encounter_gap_s(),
            "clients": self.client_count(),
            "errors": self.errors,
            "weapons": 0 if store is None else len(store.records),
        }

    def _current_session_name(self) -> str | None:
        path = self.sources.session_path()
        if path is None:
            path = default_session_path(self.sources.log_dir)
        return None if path is None else path.name

    def _session_for(self, query: dict[str, list[str]]) -> Any:
        """The session named in the query, or the current one; None when there is none."""
        name = _first(query, "session", "")
        if name:
            path = session_path(self.sources.log_dir, name)
        else:
            path = self.sources.session_path()
            if path is None:
                path = default_session_path(self.sources.log_dir)
        if path is None or not path.is_file():
            return None
        return self._sessions.get(path)

    def _count_error(self, message: str) -> None:
        self.errors += 1
        self._log(f"dashboard error #{self.errors}: {message}")


# ------------------------------------------------------------------ helpers

def _hit_key(event: Message) -> str:
    """What a hit is summed under for the chart's "now": the gun's item id, or the label of
    a non-weapon source (Skill, Grenade, ...), which is also the table row's label."""
    if event.get("src") == SOURCE_WEAPON:
        return str(event.get("wid", ""))
    return str(event.get("weapon", ""))


def _window_name(window: float) -> str:
    return str(int(window))


def _nearest_window(seconds: float) -> float:
    return min(CHART_WINDOWS_S, key=lambda window: abs(window - seconds))


def _instant_for(instant: dict[str, dict[str, float]], hit_key: str) -> dict[str, float]:
    """One value per chart window for a hit key ("" is the total)."""
    return {name: values.get(hit_key, 0.0) for name, values in instant.items()}


def _window_sample(rows: list[TableRow], now: float, instant: dict[str, dict[str, float]], slot_item_ids: list[str]) -> Message | None:
    """A chart sample: the window row's DPS cell (the fight's rate) and damage, the DPS of
    the last seconds ("now", one value per chart window), and both for every source row.
    The weapon slots (the rows right after the header, in slot order) carry their slot
    number, so the page gives each slot a fixed colour whatever gun is in it; the other
    sources are keyed by their label. A gun swapped into a slot starts a new line. None
    without a window row."""
    window_row = None
    sources: list[Message] = []
    for position, row in enumerate(rows):
        if len(row.cells) <= DPS_CELL:
            continue
        label = str(row.cells[0])
        if row.style == STYLE_TOTAL and label.startswith(WINDOW_ROW_PREFIX):
            window_row = row
        elif row.style in SOURCE_STYLES and label != EMPTY_SLOT_LABEL:
            slot = position - 1 if 1 <= position <= WEAPON_SLOT_ROWS else -1
            if slot >= 0:
                key = f"slot{slot}:{label}"
                hit_key = slot_item_ids[slot] if slot < len(slot_item_ids) else ""
            else:
                key = label
                hit_key = label
            sources.append({
                "key": key,
                "label": label,
                "slot": slot,
                # The gun's item id for the slot rows, so the page can name the line after
                # the item once the name is known.
                "item": hit_key if slot >= 0 else "",
                "dps": parse_number(row.cells[DPS_CELL]),
                "now": _instant_for(instant, hit_key) if hit_key else {name: 0.0 for name in instant},
            })
    if window_row is None:
        return None
    return {
        "t": now,
        "dps": parse_number(window_row.cells[DPS_CELL]),
        "total": parse_number(window_row.cells[TOTAL_CELL]),
        "now": _instant_for(instant, ""),
        "sources": sources,
    }


def _page_bytes() -> bytes:
    """The dashboard page, read on every request so edits show up without a restart. Works
    from the .sdkmod zip too, through importlib.resources."""
    return resources.files(PAGE_PACKAGE).joinpath(PAGE_FILE).read_bytes()


def _response(status: HTTPStatus, content_type: str, body: bytes, cache_control: str = "no-store") -> Response:
    headers = Headers([
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", cache_control),
        ("Connection", "close"),
    ])
    return Response(status.value, status.phrase, headers, body)


def _json_response(payload: Any, status: HTTPStatus = HTTPStatus.OK) -> Response:
    return _response(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))


def _text_response(status: HTTPStatus, text: str) -> Response:
    return _response(status, "text/plain; charset=utf-8", text.encode("utf-8"))


def _origin_is_foreign(request: Request) -> bool:
    """True when the browser says the page came from somewhere other than this server. The
    Origin header is set by the browser and cannot be forged by a page; requests without it
    (scripts, tools) are allowed."""
    origin = request.headers.get("Origin")
    host = request.headers.get("Host")
    if origin is None or host is None:
        return False
    return urllib.parse.urlsplit(origin).netloc.lower() != host.lower()


def lan_address() -> str:
    """This machine's address on the network, or '' when there is none."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(PROBE_ADDRESS)
            return str(probe.getsockname()[0])
    except OSError:
        return ""


def _first(query: dict[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    return values[0] if values else default


def _int(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(_first(query, name, str(default)))
    except ValueError:
        return default


def _close_quietly(connection: ServerConnection) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001
        pass


class _ForwardingHandler(logging.Handler):
    """Hands the library's warnings and errors to the mod's log line by line."""

    def __init__(self, log: Callable[[str], None]) -> None:
        super().__init__(logging.WARNING)
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log(f"dashboard ({record.levelname.lower()}): {record.getMessage()}")
        except Exception:  # noqa: BLE001 - logging must never raise into the library
            pass


def _library_logger(log: Callable[[str], None]) -> logging.Logger:
    logger = logging.getLogger("dmgtracker.web")
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    if not any(isinstance(handler, _ForwardingHandler) for handler in logger.handlers):
        logger.addHandler(_ForwardingHandler(log))
    return logger
