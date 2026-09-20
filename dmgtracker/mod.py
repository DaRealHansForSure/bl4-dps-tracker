"""
SDK glue: options, the per-frame hook, enable/disable, and the periodic status line.

Everything that touches the SDK lives here (readout.py is the other exception), so the
other modules stay importable outside the game. Kept deliberately thin: the hook callback
hands the frame to the poller, the poller hands new events to the session log and the
stats engine, and the readout is refreshed from the same frame.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR, BoolOption, ButtonOption, HiddenOption, build_mod, command, get_pc
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from .attribution import UNKNOWN_SOURCE, WEAPON_CLASS_NAME, WeaponResolver
from .logwriter import SessionLog
from .poller import DamagePoller
from .readout import Readout, ReadoutLayout
from .stats import EncounterTracker, RollingStats, source_key
from .table import build_rows
from .weaponstats import WeaponStore

LOG_PREFIX = "[dmgtracker]"
HOOK_ID = "dmgtracker"
LOG_SCHEMA_VERSION = 1
SETTINGS_FILE = SETTINGS_DIR / "dmgtracker.json"

# User data belongs under the user's profile, not in the game folder under Program Files.
DEFAULT_LOG_DIR = r"%LOCALAPPDATA%\BL4DamageTracker\logs"
FALLBACK_LOG_DIR = Path.home() / "AppData" / "Local" / "BL4DamageTracker" / "logs"

DEFAULT_TICK_FUNCTION = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
# Optional hooks that attribute explosions, grenades and melee (probes, findings section B).
# Missing ones are reported once and skipped. None of them reads hook arguments: reading an
# argument of an unknown property type inside a hook crashed the game once.
PROJECTILE_EXPLODE_FUNCTION = "/Script/GbxGame.projectile:Explode"
GRENADE_THROW_FUNCTION = "/Script/OakGame.OakCharacter:ThrowGadget"
GRENADE_EXPLODE_FUNCTION = "/Script/OakGame.GrenadeGadget:ExplodeCallback"
MELEE_SWING_FUNCTION = (
    "/Game/PlayerCharacters/_Shared/Tricks/Skills/Script_LTrick_HandleOffhand."
    "Script_LTrick_HandleOffhand_C:OnBegin_Const"
)
DEFAULT_RING_PROPERTY = "RecentlyCausedDamageInstances"
DEFAULT_RING_SIZE = 6
DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_SESSIONS_TO_KEEP = 20
DEFAULT_STATUS_INTERVAL_S = 60.0
# Seconds without a hit that end an encounter. The spec's 5 s closed fights during weapon
# swaps and grenade throws in the first real sessions; 15 s keeps a fight together.
DEFAULT_ENCOUNTER_GAP_S = 15.0

DEFAULT_READOUT_X = 120.0
DEFAULT_READOUT_Y = 320.0
DEFAULT_READOUT_FONT_SIZE = 30
DEFAULT_READOUT_COLUMN_GAP = 44.0
DEFAULT_READOUT_COLUMN_WIDTHS = [320.0, 240.0, 190.0, 140.0]
DEFAULT_READOUT_BACKGROUND_ALPHA = 0.6
DEFAULT_READOUT_WINDOW_MINUTES = 5.0
READOUT_REFRESH_INTERVAL_S = 0.25
EQUIPMENT_REFRESH_INTERVAL_S = 1.0
# A held-time sample longer than this means the game was paused or loading, not playing.
MAX_HELD_SAMPLE_S = 1.0
# Consecutive refresh failures before the table is taken down for the session. A failure
# during a map change is normal; a table that fails every frame is not.
READOUT_ERRORS_BEFORE_GIVING_UP = 20
READOUT_ERRORS_TO_LOG = 3
MAX_LIVE_ENCOUNTERS = 1000
WEAPON_STORE_NAME = "weapons.json"

# ------------------------------------------------------------------ options

logging_enabled = BoolOption(
    "Logging enabled",
    True,
    description="Write every damage instance you deal to the session log.",
)
show_readout = BoolOption(
    "Show damage table",
    True,
    description="Show the per-weapon damage table on screen.",
    on_change_while_enabled=lambda _option, value: _on_show_readout_changed(value),
)
open_log_folder = ButtonOption(
    "Open log folder",
    description="Opens the session log folder in Explorer.",
    on_press=lambda _button: _open_log_folder(),
)

# Mechanism and layout knobs. Hidden from the menu, persisted in the settings file, editable
# by hand: after a game patch the fix should be a settings edit, not a code change.
log_dir = HiddenOption("log_dir", DEFAULT_LOG_DIR)
tick_function = HiddenOption("tick_function", DEFAULT_TICK_FUNCTION)
ring_property = HiddenOption("ring_property", DEFAULT_RING_PROPERTY)
ring_size = HiddenOption("ring_size", DEFAULT_RING_SIZE)
flush_interval_s = HiddenOption("flush_interval_s", DEFAULT_FLUSH_INTERVAL_S)
sessions_to_keep = HiddenOption("sessions_to_keep", DEFAULT_SESSIONS_TO_KEEP)
status_interval_s = HiddenOption("status_interval_s", DEFAULT_STATUS_INTERVAL_S)
encounter_gap_s = HiddenOption("encounter_gap_s", DEFAULT_ENCOUNTER_GAP_S)
readout_x = HiddenOption("readout_x", DEFAULT_READOUT_X)
readout_y = HiddenOption("readout_y", DEFAULT_READOUT_Y)
readout_font_size = HiddenOption("readout_font_size", DEFAULT_READOUT_FONT_SIZE)
readout_column_gap = HiddenOption("readout_column_gap", DEFAULT_READOUT_COLUMN_GAP)
readout_column_widths = HiddenOption("readout_column_widths", DEFAULT_READOUT_COLUMN_WIDTHS)
readout_background_alpha = HiddenOption("readout_background_alpha", DEFAULT_READOUT_BACKGROUND_ALPHA)
readout_window_minutes = HiddenOption("readout_window_minutes", DEFAULT_READOUT_WINDOW_MINUTES)
readout_show_encounter_row = HiddenOption("readout_show_encounter_row", False)

# ------------------------------------------------------------------ state

resolver = WeaponResolver()
poller: DamagePoller | None = None
session_log: SessionLog | None = None
tracker = EncounterTracker(max_closed=MAX_LIVE_ENCOUNTERS)
rolling = RollingStats(DEFAULT_READOUT_WINDOW_MINUTES * 60.0)
weapon_store: WeaponStore | None = None
readout: Readout | None = None
_last_status_time = 0.0
_broken_reported = False
_readout_errors = 0
_tick_errors = 0
_last_readout_refresh = 0.0
_last_equipment_refresh = 0.0
_last_held_sample = 0.0
_transition_hidden = False
_equipped_cache: list = []
# Seconds of fighting so far: the window's clock. It only advances while an encounter is
# open, so the table stands still in menus, vehicles and between fights.
# Clocks: intervals and this combat clock run on time.monotonic(), which cannot jump; event
# timestamps and everything persisted use time.time(), which is comparable across sessions.
_combat_clock = 0.0
_last_combat_step = 0.0


def _say(message: str) -> None:
    logging.info(f"{LOG_PREFIX} {message}")


def _mod_version() -> str:
    # build_mod injects __version__ from pyproject.toml into this module before the mod can
    # be enabled, so it is available here even though no assignment is visible in the source.
    return str(globals().get("__version__", "0.0.0"))


def _log_dir() -> Path:
    """The log folder from the settings, environment variables expanded. If a variable did
    not expand (a stripped-down environment), fall back to the profile folder directly."""
    configured = os.path.expandvars(str(log_dir.value))
    if "%" in configured:
        return FALLBACK_LOG_DIR
    return Path(configured)


def _open_log_folder() -> None:
    folder = _log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.startfile(str(folder))  # noqa: S606 - Windows only, and that is the only platform BL4 runs on


# ------------------------------------------------------------------ per frame

def _on_new_events(events: list[dict[str, Any]]) -> None:
    if session_log is not None and logging_enabled.value:
        session_log.append(events)
    for event in events:
        tracker.add(event)
        rolling.add(event, at=_combat_clock)
        if weapon_store is not None:
            weapon_store.add_event(event)


def _on_tick(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    """The per-frame entry point. Every branch below handles its own errors; this outer
    guard exists because an exception escaping a hook callback would take the game down,
    and that must never happen whatever else goes wrong."""
    global _tick_errors
    if poller is None:
        return
    try:
        poller.tick()
        _refresh_readout()
        _maybe_report_status()
    except Exception as ex:  # noqa: BLE001 - last line of defence, see the docstring
        _tick_errors += 1
        if _tick_errors <= READOUT_ERRORS_TO_LOG:
            logging.error(f"{LOG_PREFIX} unexpected error in the frame hook: {type(ex).__name__}: {ex}")


# ------------------------------------------------------------------ readout

def _readout_layout() -> ReadoutLayout:
    widths = [float(width) for width in readout_column_widths.value]
    if len(widths) != 4:
        widths = list(DEFAULT_READOUT_COLUMN_WIDTHS)
    return ReadoutLayout(
        x=float(readout_x.value),
        y=float(readout_y.value),
        font_size=int(readout_font_size.value),
        column_gap=float(readout_column_gap.value),
        column_widths=(widths[0], widths[1], widths[2], widths[3]),
        background_alpha=float(readout_background_alpha.value),
    )


def _on_controller_changed(controller: UObject | None) -> None:
    """A new controller means a new world: the old widget is gone with it, build a fresh one.
    None means the controller is gone (main menu, loading): the widget goes with it, so
    nothing of ours outlives the world it was made for."""
    global _equipped_cache
    _equipped_cache = []
    _destroy_readout()
    if controller is not None and show_readout.value:
        _create_readout(controller)


def _create_readout(controller: UObject) -> None:
    global readout, _readout_errors
    if _readout_errors >= READOUT_ERRORS_BEFORE_GIVING_UP:
        return
    candidate = Readout(_readout_layout())
    try:
        candidate.create(controller)
    except Exception as ex:  # noqa: BLE001 - UMG failures must not take the tracker down
        _readout_errors += 1
        if _readout_errors <= READOUT_ERRORS_TO_LOG:
            logging.error(f"{LOG_PREFIX} cannot create the damage table: {type(ex).__name__}: {ex}")
        _give_up_readout_if_needed()
        return
    readout = candidate


def _destroy_readout() -> None:
    global readout
    if readout is None:
        return
    try:
        readout.destroy()
    except Exception as ex:  # noqa: BLE001 - the widget may already be gone with its world
        _say(f"damage table removal: {type(ex).__name__}: {ex}")
    readout = None


def _on_show_readout_changed(value: bool) -> None:
    if value:
        if poller is not None and poller.controller is not None:
            _create_readout(poller.controller)
    else:
        _destroy_readout()


def _refresh_readout() -> None:
    """Hide in menus every frame, refresh the numbers every READOUT_REFRESH_INTERVAL_S.
    Uses only this frame's controller wrapper; nothing is kept across frames."""
    global _last_readout_refresh, _last_equipment_refresh, _equipped_cache, _readout_errors, _transition_hidden
    if readout is None or poller is None or poller.controller is None:
        return
    try:
        controller = poller.controller
        pawn = controller.Pawn
        readout.set_visible(_should_show_table(controller, pawn))
        now = time.monotonic()
        if now - _last_readout_refresh < READOUT_REFRESH_INTERVAL_S:
            return
        _last_readout_refresh = now
        wall_now = time.time()
        if not controller.IsMoveInputIgnored():
            _transition_hidden = False
        else:
            # Tunnel transitions and cutscenes take the controls away; keep the table off
            # until they come back. Checked at refresh rate: a function call, not a field read.
            _transition_hidden = True
            readout.set_visible(False)
        tracker.close_if_idle(wall_now)
        _advance_combat_clock(now)
        on_foot = resolver.is_character(pawn)
        if on_foot and now - _last_equipment_refresh >= EQUIPMENT_REFRESH_INTERVAL_S:
            _last_equipment_refresh = now
            equipped = resolver.equipped_weapons(pawn)
            if equipped is not None:
                _equipped_cache = equipped
        held = resolver.weapon_in_hand(pawn) if on_foot else UNKNOWN_SOURCE
        poller.note_weapon_in_hand(wall_now, held)
        _sample_held_time(held)
        rolling.prune(_combat_clock)
        encounter = tracker.latest() if readout_show_encounter_row.value else None
        rows = build_rows(rolling, float(readout_window_minutes.value), _equipped_cache, held.instance_id, encounter)
        readout.render(rows)
        _readout_errors = 0
    except Exception as ex:  # noqa: BLE001 - keep the tracker alive even if the table breaks
        _readout_errors += 1
        if _readout_errors <= READOUT_ERRORS_TO_LOG:
            _say(f"damage table error #{_readout_errors}: {type(ex).__name__}: {ex}")
        _give_up_readout_if_needed()


def _should_show_table(controller: UObject, pawn: Any) -> bool:
    """Field reads only, so this can run every frame: no pawn (menus, loading), a vehicle, a
    menu with the cursor (ESC, inventory, map), a hidden pawn (tunnels, cutscenes) all hide
    the table."""
    if not resolver.is_character(pawn) or controller.bShowMouseCursor or _transition_hidden:
        return False
    return not pawn.bHidden


def _advance_combat_clock(now: float) -> None:
    """Run the combat clock while an encounter is open. A long gap between samples is a
    pause or a loading screen, not fighting, and is dropped."""
    global _last_held_sample, _combat_clock, _last_combat_step
    elapsed = now - _last_held_sample
    _last_held_sample = now
    _last_combat_step = 0.0
    if tracker.current is None or elapsed <= 0 or elapsed > MAX_HELD_SAMPLE_S:
        return
    _combat_clock += elapsed
    _last_combat_step = elapsed


def _sample_held_time(held: Any) -> None:
    """Credit the weapon in hand with the fighting time since the last sample, so its DPS
    can be measured over the time it was actually used in fights. Menus, vehicles and
    transitions hide the table and do not count; neither does carrying a gun around
    between fights, which would otherwise dilute its number."""
    if _last_combat_step <= 0 or not readout.visible() or held.instance_id == 0:
        return
    key = source_key({"src": held.kind, "weapon": held.label, "wid": held.instance_id})
    rolling.add_held(_combat_clock, key, _last_combat_step)
    if weapon_store is not None:
        weapon_store.add_held(held.instance_id, held.label, held.body, _last_combat_step, time.time())


def _give_up_readout_if_needed() -> None:
    if _readout_errors >= READOUT_ERRORS_BEFORE_GIVING_UP:
        _destroy_readout()
        logging.error(f"{LOG_PREFIX} damage table disabled for this session after {_readout_errors} errors in a row")


def _maybe_report_status() -> None:
    global _last_status_time, _broken_reported
    now = time.monotonic()
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
        f"{poller.events} events, {poller.ignored_events} ignored (vehicle), {poller.errors} errors, "
        f"{poller.overflow_frames} overflow-suspect frames, {poller.ticks} ticks, {_tick_errors} hook errors; "
        f"log: {log_state}"
    )
    for error in poller.recent_errors:
        _say(f"   recent error: {error}")
    poller.recent_errors.clear()


# ------------------------------------------------------------------ source hooks

def _is_player_pawn(actor: Any) -> bool:
    """Whether this actor is the player's own pawn; enemies throw gadgets too."""
    try:
        pawn = get_pc(possibly_loading=True).Pawn
        return pawn is not None and int(pawn._get_address()) == int(actor._get_address())
    except Exception:  # noqa: BLE001 - no pawn, no player action
        return False


def _on_projectile_exploded(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    """A projectile went off. Owned by a weapon: that weapon's damage follows within a few
    frames, whatever is in hand. Owned by the character: a grenade."""
    try:
        if poller is None:
            return
        instigator = obj.GetInstigator()
        if instigator is None or not _is_player_pawn(instigator):
            return
        owner = obj.GetOwner()
        if owner is not None and str(owner.Class.Name) == WEAPON_CLASS_NAME:
            poller.note_explosion(time.time(), resolver.resolve_weapon(owner))
        else:
            poller.note_grenade_exploded(time.time())
    except Exception:  # noqa: BLE001 - a hook may never raise into the game
        pass


def _on_grenade_thrown(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        if poller is not None and _is_player_pawn(obj):
            poller.note_grenade_thrown(time.time())
    except Exception:  # noqa: BLE001 - a hook may never raise into the game
        pass


def _on_grenade_exploded(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        if poller is None:
            return
        instigator = getattr(obj, "Instigator", None)
        if instigator is None or _is_player_pawn(instigator):
            poller.note_grenade_exploded(time.time())
    except Exception:  # noqa: BLE001 - a hook may never raise into the game
        pass


def _on_melee_swing(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        if poller is None:
            return
        try:
            actor = obj.GetAssociatedActor()
        except Exception:  # noqa: BLE001 - if the script cannot say whose it is, assume ours
            actor = None
        if actor is None or _is_player_pawn(actor):
            poller.note_melee_swing(time.time())
    except Exception:  # noqa: BLE001 - a hook may never raise into the game
        pass


SOURCE_HOOKS = (
    (PROJECTILE_EXPLODE_FUNCTION, "projectile-explode", _on_projectile_exploded, "explosive round"),
    (GRENADE_THROW_FUNCTION, "grenade-throw", _on_grenade_thrown, "grenade"),
    (GRENADE_EXPLODE_FUNCTION, "grenade-explode", _on_grenade_exploded, "grenade"),
    (MELEE_SWING_FUNCTION, "melee-swing", _on_melee_swing, "melee"),
)


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


def _install_source_hooks() -> None:
    for path, name, callback, damage_kind in SOURCE_HOOKS:
        try:
            unrealsdk.find_object("Function", path)
        except ValueError:
            _say(f"{name} hook target {path!r} not found; {damage_kind} damage will count as Other")
            continue
        unrealsdk.hooks.add_hook(path, Type.POST, f"{HOOK_ID}:{name}", callback)


def _remove_tick_hook() -> None:
    path = str(tick_function.value)
    if unrealsdk.hooks.has_hook(path, Type.POST, HOOK_ID):
        unrealsdk.hooks.remove_hook(path, Type.POST, HOOK_ID)
    for source_path, name, _callback, _kind in SOURCE_HOOKS:
        identifier = f"{HOOK_ID}:{name}"
        if unrealsdk.hooks.has_hook(source_path, Type.POST, identifier):
            unrealsdk.hooks.remove_hook(source_path, Type.POST, identifier)


# ------------------------------------------------------------------ lifecycle

def on_enable() -> None:
    global poller, session_log, tracker, rolling, weapon_store
    global _last_status_time, _broken_reported, _readout_errors, _last_held_sample, _combat_clock
    _broken_reported = False
    _readout_errors = 0
    _last_status_time = time.monotonic()
    _last_held_sample = time.monotonic()
    _combat_clock = 0.0
    tracker = EncounterTracker(gap_s=float(encounter_gap_s.value), max_closed=MAX_LIVE_ENCOUNTERS)
    rolling = RollingStats(float(readout_window_minutes.value) * 60.0)
    weapon_store = _load_weapon_store()
    poller = DamagePoller(
        resolve_controller=lambda: get_pc(possibly_loading=True),
        ring_property=str(ring_property.value),
        ring_size=int(ring_size.value),
        resolver=resolver,
        sink=_on_new_events,
    )
    poller.on_controller_changed = _on_controller_changed
    session_log = _start_session_log()
    if session_log is not None and weapon_store is not None:
        # The writer thread wakes every flush interval; let it save the weapon record too.
        session_log.add_flush_listener(lambda _batch: weapon_store.save_if_due(time.time()))
    if not _install_tick_hook():
        return
    _install_source_hooks()
    log_path = session_log.path if session_log is not None else "nothing (log unavailable)"
    _say(f"v{_mod_version()} enabled, logging to {log_path}")


def _load_weapon_store() -> WeaponStore:
    store = WeaponStore(_log_dir().parent / WEAPON_STORE_NAME)
    store.load()
    if store.load_error:
        logging.error(f"{LOG_PREFIX} weapon record {store.path} could not be read ({store.load_error}); starting a new one")
    return store


def _start_session_log() -> SessionLog | None:
    meta = {
        "mod": "dmgtracker",
        "version": _mod_version(),
        "schema": LOG_SCHEMA_VERSION,
        "started": time.time(),
        "encounter_gap_s": float(encounter_gap_s.value),
    }
    folder = _log_dir()
    log = SessionLog(folder, float(flush_interval_s.value), int(sessions_to_keep.value), meta)
    try:
        log.start()
    except OSError as ex:
        logging.error(f"{LOG_PREFIX} cannot create the session log in {folder}: {ex}. Running without a log.")
        return None
    return log


def on_disable() -> None:
    global poller, session_log, weapon_store
    _remove_tick_hook()
    _destroy_readout()
    if weapon_store is not None:
        weapon_store.save_if_due(time.time(), force=True)
        if weapon_store.save_error:
            logging.error(f"{LOG_PREFIX} weapon record not saved: {weapon_store.save_error}")
        weapon_store = None
    if session_log is not None:
        session_log.stop()
        _say(f"disabled; {session_log.written} events written to {session_log.path}")
    if poller is not None:
        _say(f"{poller.events} events seen, {poller.errors} errors, {poller.ticks} ticks")
    poller = None
    session_log = None


# ------------------------------------------------------------------ console commands

@command("dmgweapons", description="Rank your weapons by damage per second in hand, over all sessions.")
def dmgweapons_command(args: argparse.Namespace) -> None:
    if weapon_store is None:
        logging.info(f"{LOG_PREFIX} the mod is not enabled")
        return
    for line in weapon_store.format_ranking(limit=args.limit):
        logging.info(line)


dmgweapons_command.add_argument("-n", "--limit", type=int, default=15, help="how many weapons to list")


mod = build_mod(
    settings_file=SETTINGS_FILE,
    options=[
        logging_enabled,
        show_readout,
        open_log_folder,
        log_dir,
        tick_function,
        ring_property,
        ring_size,
        flush_interval_s,
        sessions_to_keep,
        status_interval_s,
        encounter_gap_s,
        readout_x,
        readout_y,
        readout_font_size,
        readout_column_gap,
        readout_column_widths,
        readout_background_alpha,
        readout_window_minutes,
        readout_show_encounter_row,
    ],
    keybinds=[],
    hooks=[],
    commands=[dmgweapons_command],
    on_enable=on_enable,
    on_disable=on_disable,
)
