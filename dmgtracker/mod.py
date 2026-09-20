"""
SDK glue: options, the per-frame hook, enable/disable, and the periodic status line.

Everything that touches the SDK lives here, so the other modules stay importable outside
the game. Kept deliberately thin: the hook callback hands the frame to the poller, the
poller hands new events to the session log (and, in the next step, to the readout).
"""

from __future__ import annotations

import os
import time
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR, BoolOption, ButtonOption, HiddenOption, build_mod, get_pc
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from .attribution import WeaponResolver
from .logwriter import SessionLog
from .poller import DamagePoller

LOG_PREFIX = "[dmgtracker]"
HOOK_ID = "dmgtracker"
LOG_SCHEMA_VERSION = 1
SETTINGS_FILE = SETTINGS_DIR / "dmgtracker.json"
LOG_DIR = SETTINGS_DIR / "dmgtracker" / "logs"

DEFAULT_TICK_FUNCTION = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
DEFAULT_RING_PROPERTY = "RecentlyCausedDamageInstances"
DEFAULT_RING_SIZE = 6
DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_SESSIONS_TO_KEEP = 20
DEFAULT_STATUS_INTERVAL_S = 60.0

# ------------------------------------------------------------------ options

logging_enabled = BoolOption(
    "Logging enabled",
    True,
    description="Write every damage instance you deal to the session log.",
)
open_log_folder = ButtonOption(
    "Open log folder",
    description=f"Opens {LOG_DIR} in Explorer.",
    on_press=lambda _button: _open_log_folder(),
)

# Mechanism and layout knobs. Hidden from the menu, persisted in the settings file, editable
# by hand: after a game patch the fix should be a settings edit, not a code change.
tick_function = HiddenOption("tick_function", DEFAULT_TICK_FUNCTION)
ring_property = HiddenOption("ring_property", DEFAULT_RING_PROPERTY)
ring_size = HiddenOption("ring_size", DEFAULT_RING_SIZE)
flush_interval_s = HiddenOption("flush_interval_s", DEFAULT_FLUSH_INTERVAL_S)
sessions_to_keep = HiddenOption("sessions_to_keep", DEFAULT_SESSIONS_TO_KEEP)
status_interval_s = HiddenOption("status_interval_s", DEFAULT_STATUS_INTERVAL_S)

# ------------------------------------------------------------------ state

resolver = WeaponResolver()
poller: DamagePoller | None = None
session_log: SessionLog | None = None
_last_status_time = 0.0
_broken_reported = False


def _say(message: str) -> None:
    logging.info(f"{LOG_PREFIX} {message}")


def _mod_version() -> str:
    # build_mod injects __version__ from pyproject.toml into this module before the mod can
    # be enabled, so it is available here even though no assignment is visible in the source.
    return str(globals().get("__version__", "0.0.0"))


def _open_log_folder() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.startfile(str(LOG_DIR))  # noqa: S606 - Windows only, and that is the only platform BL4 runs on


# ------------------------------------------------------------------ per frame

def _on_new_events(events: list[dict[str, Any]]) -> None:
    if session_log is not None and logging_enabled.value:
        session_log.append(events)


def _on_tick(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    if poller is None:
        return
    poller.tick()
    _maybe_report_status()


def _maybe_report_status() -> None:
    global _last_status_time, _broken_reported
    now = time.time()
    if now - _last_status_time < float(status_interval_s.value):
        return
    _last_status_time = now
    if poller is None:
        return
    if poller.broken_reason and not _broken_reported:
        _broken_reported = True
        logging.error(f"{LOG_PREFIX} tracking stopped: {poller.broken_reason} (settings: {SETTINGS_FILE})")
    log_state = "no log"
    if session_log is not None:
        log_state = (
            f"{session_log.written} written, {session_log.buffered()} buffered, "
            f"{session_log.write_errors} write errors"
        )
    _say(
        f"{poller.events} events, {poller.errors} errors, {poller.overflow_frames} overflow-suspect "
        f"frames, {poller.ticks} ticks; log: {log_state}"
    )
    for error in poller.recent_errors:
        _say(f"   recent error: {error}")
    poller.recent_errors.clear()


# ------------------------------------------------------------------ hook management

def _install_tick_hook() -> bool:
    """Hook the configured per-frame function. False, with one clear error, when the game no
    longer has it; the mod then stays enabled but idle instead of failing every frame."""
    path = str(tick_function.value)
    try:
        unrealsdk.find_object("Function", path)
    except ValueError:
        logging.error(
            f"{LOG_PREFIX} tick function {path!r} does not exist in this game build. Tracking is "
            f"off. Fix tick_function in {SETTINGS_FILE} or wait for a mod update."
        )
        return False
    unrealsdk.hooks.add_hook(path, Type.POST, HOOK_ID, _on_tick)
    return True


def _remove_tick_hook() -> None:
    path = str(tick_function.value)
    if unrealsdk.hooks.has_hook(path, Type.POST, HOOK_ID):
        unrealsdk.hooks.remove_hook(path, Type.POST, HOOK_ID)


# ------------------------------------------------------------------ lifecycle

def on_enable() -> None:
    global poller, session_log, _last_status_time, _broken_reported
    _broken_reported = False
    _last_status_time = time.time()
    poller = DamagePoller(
        resolve_controller=lambda: get_pc(possibly_loading=True),
        ring_property=str(ring_property.value),
        ring_size=int(ring_size.value),
        resolver=resolver,
        sink=_on_new_events,
    )
    session_log = _start_session_log()
    if not _install_tick_hook():
        return
    log_path = session_log.path if session_log is not None else "nothing (log unavailable)"
    _say(f"v{_mod_version()} enabled, logging to {log_path}")


def _start_session_log() -> SessionLog | None:
    meta = {
        "mod": "dmgtracker",
        "version": _mod_version(),
        "schema": LOG_SCHEMA_VERSION,
        "started": time.time(),
    }
    log = SessionLog(LOG_DIR, float(flush_interval_s.value), int(sessions_to_keep.value), meta)
    try:
        log.start()
    except OSError as ex:
        logging.error(f"{LOG_PREFIX} cannot create the session log in {LOG_DIR}: {ex}. Running without a log.")
        return None
    return log


def on_disable() -> None:
    global poller, session_log
    _remove_tick_hook()
    if session_log is not None:
        session_log.stop()
        _say(f"disabled; {session_log.written} events written to {session_log.path}")
    if poller is not None:
        _say(f"{poller.events} events seen, {poller.errors} errors, {poller.ticks} ticks")
    poller = None
    session_log = None


mod = build_mod(
    settings_file=SETTINGS_FILE,
    options=[
        logging_enabled,
        open_log_folder,
        tick_function,
        ring_property,
        ring_size,
        flush_interval_s,
        sessions_to_keep,
        status_interval_s,
    ],
    keybinds=[],
    hooks=[],
    commands=[],
    on_enable=on_enable,
    on_disable=on_disable,
)
