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
import webbrowser
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR, BoolOption, ButtonOption, HiddenOption, build_mod, command, get_pc
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from .attribution import SOURCE_MELEE, UNKNOWN_SOURCE, WEAPON_CLASS_NAME, WeaponResolver
from .character import CharacterIdentity, read_identity
from .dashboard import DEFAULT_HOST as DEFAULT_WEB_HOST
from .dashboard import Dashboard, DashboardSources
from .formatting import format_number
from .itemidentity import ItemIdentityReader
from .logwriter import SessionLog
from .poller import DamagePoller
from .readout import Readout, ReadoutLayout
from .report import format_report, gap_for, read_session
from .stats import EncounterTracker, RollingStats, kind_damage, source_key
from .table import PeakDps, build_rows
from .weaponstats import PEAK_SOURCE_KINDS, WeaponStore

LOG_PREFIX = "[dmgtracker]"
HOOK_ID = "dmgtracker"
# 2: `wid` and `held` carry the item id (a hex seed, or "instance:<n>" as fallback) instead of the numeric instance id.
LOG_SCHEMA_VERSION = 2
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
# Every trick starts and ends through these native functions. The player's melee tricks are
# the ATrick_Melee_* scripts (shared, stateless objects, so they cannot say whose swing it
# is, but enemy melee tricks are named differently); action skill tricks carry "ActionSkill"
# in their name (probes 2026-09-20, three characters).
TRICK_BEGIN_FUNCTION = "/Script/GbxGame.GbxTrickScript:OnBegin_Mut"
TRICK_END_FUNCTION = "/Script/GbxGame.GbxTrickScript:OnEnd_Mut"
# Every skill script action (a revolver volley, a dice throw, a Reaper swing) starts through
# this native function on the script object, whose GetOwner() is the player controller
# (probe #17, 2026-09-20). Traits and passives fire skill events but no actions.
SKILL_ACTION_FUNCTION = "/Script/GbxGame.GbxSkillScript:K2_StartSkillAction"
# The token stack: tokens spawned by a skill are added when they return, and other skills
# consume them; adding tokens is skill activity as well.
SKILL_TOKEN_FUNCTION = "/Script/OakGame.GbxSkillTokenProviderStatics:AddToTokenStack"
SHARED_PLAYER_TRICK_PREFIX = "ATrick_"
ENEMY_TRICK_PREFIX = "TRAnim_"
MELEE_TRICK_FRAGMENT = "Melee"
SKILL_TRICK_CLASS_FRAGMENT = "ActionSkill"
DEFAULT_RING_PROPERTY = "RecentlyCausedDamageInstances"
DEFAULT_RING_SIZE = 6
DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_SESSIONS_TO_KEEP = 20
DEFAULT_STATUS_INTERVAL_S = 60.0
# Seconds without a hit that end an encounter. The spec's 5 s closed fights during weapon
# swaps and grenade throws in the first real sessions; 15 s keeps a fight together.
DEFAULT_ENCOUNTER_GAP_S = 15.0

DEFAULT_READOUT_X = 32.0
DEFAULT_READOUT_Y = 320.0
DEFAULT_READOUT_FONT_SIZE = 30
DEFAULT_READOUT_COLUMN_GAP = 44.0
DEFAULT_READOUT_COLUMN_WIDTHS = [320.0, 240.0, 190.0, 140.0]
# Widths of the optional columns, in their order after the base four: Crit [%], Kills, Best DPS.
DEFAULT_CRIT_COLUMN_WIDTH = 150.0
DEFAULT_KILLS_COLUMN_WIDTH = 120.0
DEFAULT_BEST_COLUMN_WIDTH = 170.0
BASE_COLUMN_COUNT = 4
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
CHARACTERS_DIR_NAME = "characters"
READOUT_CREATE_RETRY_S = 1.0
# Time-in-hand lines in the session log, once per second per gun, so the report and the
# dashboard can compute DPS in hand from the log.
HELD_LINE_INTERVAL_S = 1.0
DEFAULT_WEB_PORT = 8765

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
web_enabled = BoolOption(
    "Web dashboard",
    True,
    description="Serve the dashboard page (live table, hits, weapon ranking, encounters); 'dmgweb' in the console prints the address.",
    on_change_while_enabled=lambda _option, value: _set_dashboard_enabled(value),
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
readout_show_best_column = HiddenOption("readout_show_best_column", True)
readout_show_crit_and_kills = HiddenOption("readout_show_crit_and_kills", True)
# Non-weapon rows to leave out of the table, by source kind: "melee", "grenade", "skill", "other".
readout_hidden_rows = HiddenOption("readout_hidden_rows", [SOURCE_MELEE])
# The dashboard listens on every interface (0.0.0.0) so other devices on the network can open
# it; "127.0.0.1" keeps it on this machine.
web_host = HiddenOption("web_host", DEFAULT_WEB_HOST)
web_port = HiddenOption("web_port", DEFAULT_WEB_PORT)

# ------------------------------------------------------------------ state

identity_reader = ItemIdentityReader()
resolver = WeaponResolver(identity_reader.item_id)
poller: DamagePoller | None = None
dashboard: Dashboard | None = None
session_log: SessionLog | None = None
tracker = EncounterTracker(max_closed=MAX_LIVE_ENCOUNTERS)
rolling = RollingStats(DEFAULT_READOUT_WINDOW_MINUTES * 60.0)
weapon_store: WeaponStore | None = None
character: CharacterIdentity | None = None
readout: Readout | None = None
_last_readout_create = 0.0
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
_latest_rows: list = []
_held_pending: dict[str, tuple[str, float]] = {}
_last_held_flush = 0.0


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
    """The base column widths come from the settings; an optional column beyond the list
    gets its default width."""
    widths = [float(width) for width in readout_column_widths.value]
    if len(widths) < BASE_COLUMN_COUNT:
        widths = list(DEFAULT_READOUT_COLUMN_WIDTHS)
    widths = widths[:BASE_COLUMN_COUNT]
    if readout_show_crit_and_kills.value:
        widths.extend([DEFAULT_CRIT_COLUMN_WIDTH, DEFAULT_KILLS_COLUMN_WIDTH])
    if readout_show_best_column.value:
        widths.append(DEFAULT_BEST_COLUMN_WIDTH)
    columns = len(widths)
    return ReadoutLayout(
        x=float(readout_x.value),
        y=float(readout_y.value),
        font_size=int(readout_font_size.value),
        column_gap=float(readout_column_gap.value),
        column_widths=tuple(widths[:columns]),
        background_alpha=float(readout_background_alpha.value),
    )


def _on_controller_changed(controller: UObject | None) -> None:
    """A new controller means a new world: the old widget is gone with it, build a fresh one.
    None means the controller is gone (main menu, loading): the widget goes with it, so
    nothing of ours outlives the world it was made for. The character is checked on the
    refresh loop, once the player state is ready."""
    global _equipped_cache
    _equipped_cache = []
    _destroy_readout()
    if controller is not None and show_readout.value:
        _create_readout(controller)


# ------------------------------------------------------------------ characters

def _character_store_path(identity: CharacterIdentity) -> Path:
    return _log_dir().parent / CHARACTERS_DIR_NAME / identity.key / WEAPON_STORE_NAME


def _check_character(controller: UObject, pawn: Any) -> None:
    """Adopt the character behind this controller; switching saves the old record, loads the
    new one, and starts the live table from zero. Called once a second while in the world."""
    global character, weapon_store
    try:
        identity = read_identity(controller.PlayerState, pawn)
    except Exception:  # noqa: BLE001 - no player state yet
        identity = None
    if identity is None or (character is not None and identity.key == character.key):
        return
    if weapon_store is not None:
        weapon_store.save_if_due(time.time(), force=True)
    character = identity
    weapon_store = _load_weapon_store(_character_store_path(identity))
    _reset_live_stats()
    if session_log is not None:
        session_log.append([{"character": identity.to_json(), "t": time.time()}])
    if dashboard is not None:
        dashboard.publish_character(identity.to_json())
    _say(f"character: {identity.display} ({identity.class_name}), key {identity.key[:8]}, record {weapon_store.path}")


def _reset_live_stats() -> None:
    global tracker, rolling, _combat_clock, _equipped_cache
    tracker = EncounterTracker(gap_s=float(encounter_gap_s.value), max_closed=MAX_LIVE_ENCOUNTERS)
    rolling = RollingStats(float(readout_window_minutes.value) * 60.0)
    _combat_clock = 0.0
    _equipped_cache = []


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
    global _last_readout_create, _latest_rows
    if poller is None or poller.controller is None:
        return
    try:
        controller = poller.controller
        pawn = controller.Pawn
        now = time.monotonic()
        if readout is None or not readout.is_created():
            # The widget died with its world, or creation failed earlier: try again, slowly.
            if show_readout.value and resolver.is_character(pawn) and now - _last_readout_create >= READOUT_CREATE_RETRY_S:
                _last_readout_create = now
                _create_readout(controller)
            if readout is None:
                return
        readout.set_visible(_should_show_table(controller, pawn))
        readout.fade(now)
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
            _check_character(controller, pawn)
            equipped = resolver.equipped_weapons(pawn)
            if equipped is not None:
                _equipped_cache = equipped
        held = resolver.weapon_in_hand(pawn) if on_foot else UNKNOWN_SOURCE
        poller.note_weapon_in_hand(wall_now, held)
        _sample_held_time(held)
        rolling.prune(_combat_clock)
        _record_peaks(wall_now)
        encounter = tracker.latest() if readout_show_encounter_row.value else None
        best = _peak_dps() if readout_show_best_column.value else None
        rows = build_rows(
            rolling,
            float(readout_window_minutes.value),
            _equipped_cache,
            held.item_id,
            encounter,
            best,
            _hidden_row_kinds(),
            bool(readout_show_crit_and_kills.value),
        )
        _latest_rows = rows
        readout.render(rows, now)
        if dashboard is not None:
            dashboard.publish_rows(rows)
        _flush_held_lines(wall_now)
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


def _peak_dps() -> PeakDps:
    if weapon_store is None:
        return PeakDps({}, {})
    return PeakDps(weapon_store.peaks(), weapon_store.source_peaks())


def _hidden_row_kinds() -> frozenset[str]:
    try:
        return frozenset(str(kind) for kind in readout_hidden_rows.value)
    except TypeError:
        return frozenset()


def _record_peaks(now: float) -> None:
    """Offer every table row's current DPS (damage over the window's fighting time) to the
    lifetime record as a peak."""
    if weapon_store is None:
        return
    window_seconds = rolling.duration()
    for totals in rolling.by_source.values():
        if totals.kind != "weapon" or totals.item_id == "":
            continue
        weapon_store.note_peak(totals.item_id, totals.label, "", totals.damage / window_seconds, window_seconds, now)
    for kind in PEAK_SOURCE_KINDS:
        damage = kind_damage(rolling.by_source, kind)
        weapon_store.note_source_peak(kind, damage / window_seconds, window_seconds, now)


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
    if _last_combat_step <= 0 or not readout.visible() or held.item_id == "":
        return
    key = source_key({"src": held.kind, "weapon": held.label, "wid": held.item_id})
    rolling.add_held(_combat_clock, key, _last_combat_step)
    if weapon_store is not None:
        weapon_store.add_held(held.item_id, held.label, held.body, _last_combat_step, time.time())
    _label, seconds = _held_pending.get(held.item_id, (held.label, 0.0))
    _held_pending[held.item_id] = (held.label, seconds + _last_combat_step)


def _flush_held_lines(now: float) -> None:
    """Write the accumulated time in hand as one compact line per gun, once a second, so the
    report and the dashboard can compute DPS in hand from the log."""
    global _last_held_flush
    if not _held_pending or now - _last_held_flush < HELD_LINE_INTERVAL_S:
        return
    _last_held_flush = now
    if session_log is not None and logging_enabled.value:
        lines = [{"t": now, "held": item_id, "weapon": label, "s": round(seconds, 3)} for item_id, (label, seconds) in _held_pending.items()]
        session_log.append(lines)
    _held_pending.clear()




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


_seen_melee_tricks: set[str] = set()


def _is_player_melee_trick(class_name: str) -> bool:
    """The player's melee tricks are the shared ATrick_* scripts or the character's own
    Trick_<Class>_* ones; enemy melee tricks are TRAnim_* and instanced on the enemy."""
    if MELEE_TRICK_FRAGMENT not in class_name or class_name.startswith(ENEMY_TRICK_PREFIX):
        return False
    if class_name.startswith(SHARED_PLAYER_TRICK_PREFIX):
        return True
    if character is not None:
        class_token = character.class_name.removeprefix("Char_")
        if class_token and class_token in class_name:
            return True
    return False


def _on_trick_begin(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    """Fires for every trick of every actor; the class name says what it is."""
    try:
        if poller is None:
            return
        class_name = str(obj.Class.Name)
        if SKILL_TRICK_CLASS_FRAGMENT in class_name:
            poller.note_skill_activity(time.time())
            return
        if MELEE_TRICK_FRAGMENT in class_name and class_name not in _seen_melee_tricks:
            # One line per new melee-like trick, so an unexplained Melee/Other case can be
            # traced back to the trick's name without a probe run.
            _seen_melee_tricks.add(class_name)
            _say(f"melee-like trick seen: {class_name} ({'counted' if _is_player_melee_trick(class_name) else 'ignored'})")
        if _is_player_melee_trick(class_name):
            poller.note_melee_swing(time.time())
    except Exception:  # noqa: BLE001 - a hook may never raise into the game
        pass


def _on_skill_action(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    """A skill script of some actor starts an action; only the local player's scripts count.
    The script's owner is the player controller, read through its no-argument getter."""
    try:
        if poller is None or poller.controller is None:
            return
        owner = obj.GetOwner()
        if owner is None or int(owner._get_address()) != int(poller.controller._get_address()):
            return
        poller.note_skill_activity(time.time())
    except Exception:  # noqa: BLE001 - a hook may never raise into the game
        pass


def _on_skill_token(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    """A static call with no owner; only the local player's skills run this code here."""
    try:
        if poller is not None:
            poller.note_skill_activity(time.time())
    except Exception:  # noqa: BLE001 - a hook may never raise into the game
        pass


def _on_trick_end(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        if poller is None:
            return
        class_name = str(obj.Class.Name)
        if SKILL_TRICK_CLASS_FRAGMENT in class_name:
            poller.note_skill_activity(time.time())
        elif _is_player_melee_trick(class_name):
            poller.note_melee_end(time.time())
    except Exception:  # noqa: BLE001 - a hook may never raise into the game
        pass


SOURCE_HOOKS = (
    (PROJECTILE_EXPLODE_FUNCTION, "projectile-explode", _on_projectile_exploded, "explosive round"),
    (GRENADE_THROW_FUNCTION, "grenade-throw", _on_grenade_thrown, "grenade"),
    (GRENADE_EXPLODE_FUNCTION, "grenade-explode", _on_grenade_exploded, "grenade"),
    (TRICK_BEGIN_FUNCTION, "trick-begin", _on_trick_begin, "melee and skill"),
    (TRICK_END_FUNCTION, "trick-end", _on_trick_end, "skill"),
    (SKILL_ACTION_FUNCTION, "skill-action", _on_skill_action, "skill"),
    (SKILL_TOKEN_FUNCTION, "skill-token", _on_skill_token, "skill"),
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
    # No existence check here: Blueprint functions (the melee trick) load with the world,
    # long after the mod enables, and unrealsdk matches hooks by name when they are called.
    for path, name, callback, _damage_kind in SOURCE_HOOKS:
        unrealsdk.hooks.add_hook(path, Type.PRE, f"{HOOK_ID}:{name}", callback)


def _remove_tick_hook() -> None:
    path = str(tick_function.value)
    if unrealsdk.hooks.has_hook(path, Type.POST, HOOK_ID):
        unrealsdk.hooks.remove_hook(path, Type.POST, HOOK_ID)
    for source_path, name, _callback, _kind in SOURCE_HOOKS:
        identifier = f"{HOOK_ID}:{name}"
        if unrealsdk.hooks.has_hook(source_path, Type.PRE, identifier):
            unrealsdk.hooks.remove_hook(source_path, Type.PRE, identifier)


# ------------------------------------------------------------------ lifecycle

def on_enable() -> None:
    global poller, session_log, tracker, rolling, weapon_store, character
    global _last_status_time, _broken_reported, _readout_errors, _last_held_sample, _combat_clock
    _broken_reported = False
    _readout_errors = 0
    _last_status_time = time.monotonic()
    _last_held_sample = time.monotonic()
    _combat_clock = 0.0
    tracker = EncounterTracker(gap_s=float(encounter_gap_s.value), max_closed=MAX_LIVE_ENCOUNTERS)
    rolling = RollingStats(float(readout_window_minutes.value) * 60.0)
    character = None
    weapon_store = None
    poller = DamagePoller(
        resolve_controller=lambda: get_pc(possibly_loading=True),
        ring_property=str(ring_property.value),
        ring_size=int(ring_size.value),
        resolver=resolver,
        sink=_on_new_events,
    )
    poller.on_controller_changed = _on_controller_changed
    session_log = _start_session_log()
    if session_log is not None:
        # The writer thread wakes every flush interval; let it save the weapon record too.
        session_log.add_flush_listener(_save_weapon_store_if_due)
        session_log.add_flush_listener(_publish_log_batch)
    if web_enabled.value:
        _start_dashboard()
    if not _install_tick_hook():
        return
    _install_source_hooks()
    log_path = session_log.path if session_log is not None else "nothing (log unavailable)"
    _say(f"v{_mod_version()} enabled, logging to {log_path}")


def _load_weapon_store(path: Path) -> WeaponStore:
    store = WeaponStore(path)
    store.load()
    if store.load_error:
        logging.error(f"{LOG_PREFIX} weapon record {store.path} could not be read ({store.load_error}); starting a new one")
    if store.set_aside_old_schema is not None:
        _say(f"weapon record in the old format (keyed by the unstable instance id) set aside as {store.set_aside_old_schema.name}; starting a new one")
    if store.peaks_dropped:
        _say(f"weapon record: {store.peaks_dropped} Best DPS value(s) measured as DPS in hand dropped; Best DPS is window DPS now")
    return store


def _publish_log_batch(batch: list[dict[str, Any]]) -> None:
    """Flush listener (writer thread): hands every written batch to the dashboard."""
    if dashboard is not None:
        dashboard.publish_lines(batch)


def _dashboard_sources() -> DashboardSources:
    return DashboardSources(
        version=_mod_version(),
        log_dir=_log_dir(),
        weapon_store=lambda: weapon_store,
        character=lambda: None if character is None else character.to_json(),
        session_path=lambda: None if session_log is None else session_log.path,
        window_minutes=lambda: float(readout_window_minutes.value),
        encounter_gap_s=lambda: float(encounter_gap_s.value),
    )


def _start_dashboard() -> None:
    global dashboard
    if dashboard is not None:
        return
    candidate = Dashboard(int(web_port.value), _dashboard_sources(), _say, host=str(web_host.value))
    if not candidate.start():
        logging.error(f"{LOG_PREFIX} dashboard not started on {web_host.value}:{web_port.value}: {candidate.start_error}")
        return
    dashboard = candidate
    _say(f"dashboard at {' or '.join(candidate.urls())}")


def _stop_dashboard() -> None:
    global dashboard
    if dashboard is None:
        return
    try:
        dashboard.stop()
    except Exception as ex:  # noqa: BLE001 - shutting down must not fail the disable
        logging.error(f"{LOG_PREFIX} dashboard stop: {type(ex).__name__}: {ex}")
    dashboard = None


def _set_dashboard_enabled(enabled: bool) -> None:
    if enabled:
        _start_dashboard()
    else:
        _stop_dashboard()


def _save_weapon_store_if_due(_batch: list[dict[str, Any]]) -> None:
    store = weapon_store
    if store is not None:
        store.save_if_due(time.time())


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
    _stop_dashboard()
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

@command("dmgweapons", description="Rank the current character's weapons by damage per second in hand, over all sessions.")
def dmgweapons_command(args: argparse.Namespace) -> None:
    if weapon_store is None or character is None:
        logging.info(f"{LOG_PREFIX} no character loaded yet (the mod must be enabled and you must be in the world)")
        return
    logging.info(f"{LOG_PREFIX} weapons of {character.display} ({character.class_name}), by {args.by}:")
    for line in weapon_store.format_ranking(limit=args.limit, by=args.by):
        logging.info(line)
    for line in weapon_store.format_source_peaks():
        logging.info(line)


dmgweapons_command.add_argument("-n", "--limit", type=int, default=15, help="how many weapons to list")
dmgweapons_command.add_argument("--by", choices=("dps", "peak", "damage"), default="dps", help="sort order (default: DPS in hand)")


@command("dmgweb", description="Print the dashboard address; --open opens it in the browser.")
def dmgweb_command(args: argparse.Namespace) -> None:
    if dashboard is None:
        reason = "disabled in the mod menu" if not web_enabled.value else "not running (see the log for the reason)"
        logging.info(f"{LOG_PREFIX} dashboard {reason}")
        return
    for url in dashboard.urls():
        logging.info(f"{LOG_PREFIX} dashboard: {url}")
    logging.info(f"{LOG_PREFIX} {dashboard.client_count()} client(s) connected, {dashboard.errors} errors")
    if args.open:
        webbrowser.open(dashboard.url)


dmgweb_command.add_argument("--open", action="store_true", help="open the dashboard in the default browser")


@command("dmgreport", description="Report over the current session: encounters, per-weapon numbers, optionally the last N seconds.")
def dmgreport_command(args: argparse.Namespace) -> None:
    if session_log is None or session_log.path is None:
        logging.info(f"{LOG_PREFIX} no session log (the mod is not enabled or logging failed)")
        return
    try:
        session = read_session(session_log.path)
        lines = format_report(session, gap_for(session, args.gap), args.last, None if args.all else args.top)
    except Exception as ex:  # noqa: BLE001 - a report failure is worth a line, not a stack in the console
        logging.error(f"{LOG_PREFIX} report failed: {type(ex).__name__}: {ex}")
        return
    for line in lines:
        logging.info(line)


dmgreport_command.add_argument("--last", type=float, default=None, metavar="SECONDS", help="also average the final N seconds")
dmgreport_command.add_argument("--gap", type=float, default=None, metavar="SECONDS", help="seconds without a hit that end an encounter")
dmgreport_command.add_argument("--top", type=int, default=10, metavar="N", help="only the N largest encounters per character")
dmgreport_command.add_argument("--all", action="store_true", help="every encounter")


@command("dmgstatus", description="Show the tracker's state: counters, hooks, character, files.")
def dmgstatus_command(args: argparse.Namespace) -> None:
    if poller is None:
        logging.info(f"{LOG_PREFIX} not enabled")
        return
    logging.info(f"{LOG_PREFIX} v{_mod_version()}; tick hook on {tick_function.value}")
    logging.info(
        f"{LOG_PREFIX} {poller.events} events, {poller.ignored_events} ignored (vehicle), {poller.ignored_heals} heals dropped, "
        f"{poller.errors} errors, {poller.overflow_frames} overflow-suspect frames, {poller.ticks} ticks, {_tick_errors} hook errors"
    )
    if poller.broken_reason:
        logging.info(f"{LOG_PREFIX} tracking stopped: {poller.broken_reason}")
    if character is not None:
        logging.info(f"{LOG_PREFIX} character: {character.display} ({character.class_name}), key {character.key[:8]}")
    else:
        logging.info(f"{LOG_PREFIX} character: not known yet")
    if weapon_store is not None:
        logging.info(f"{LOG_PREFIX} weapon record: {weapon_store.path} ({len(weapon_store.records)} weapons)")
    identity_state = f"{identity_reader.fallbacks} fallback(s) to the instance id"
    if identity_reader.last_error:
        identity_state += f", last error: {identity_reader.last_error}"
    logging.info(f"{LOG_PREFIX} item identity: {identity_state}")
    if session_log is not None:
        logging.info(
            f"{LOG_PREFIX} session log: {session_log.path} ({session_log.written} written, {session_log.buffered()} buffered, "
            f"{session_log.write_errors} write errors)"
        )
    table_state = "shown" if readout is not None and readout.visible() else ("created, hidden" if readout is not None else "not created")
    logging.info(f"{LOG_PREFIX} table: {table_state}; combat clock {format_number(_combat_clock)} s; window {readout_window_minutes.value} min")
    if dashboard is not None:
        logging.info(f"{LOG_PREFIX} dashboard: {dashboard.url} ({dashboard.client_count()} client(s), {dashboard.errors} errors)")
    else:
        logging.info(f"{LOG_PREFIX} dashboard: not running")


@command("dmgreset", description="Reset the damage table; with --weapons also this character's lifetime record, with --all every character's.")
def dmgreset_command(args: argparse.Namespace) -> None:
    if poller is None:
        logging.info(f"{LOG_PREFIX} the mod is not enabled")
        return
    _reset_live_stats()
    logging.info(f"{LOG_PREFIX} damage table reset")
    if args.all:
        _reset_all_weapon_records()
    elif args.weapons:
        _reset_current_weapon_record()


dmgreset_command.add_argument("--weapons", action="store_true", help="also start a fresh lifetime record for the current character")
dmgreset_command.add_argument("--all", action="store_true", help="also start fresh lifetime records for every character")


def _reset_current_weapon_record() -> None:
    if weapon_store is None or character is None:
        logging.info(f"{LOG_PREFIX} no character loaded, no record to reset")
        return
    set_aside = weapon_store.reset()
    weapon_store.save_if_due(time.time(), force=True)
    logging.info(f"{LOG_PREFIX} weapon record of {character.display} reset" + (f"; old file kept as {set_aside}" if set_aside else ""))


def _reset_all_weapon_records() -> None:
    characters_dir = _log_dir().parent / CHARACTERS_DIR_NAME
    count = 0
    for record_path in characters_dir.glob(f"*/{WEAPON_STORE_NAME}"):
        if weapon_store is not None and record_path == weapon_store.path:
            continue
        WeaponStore(record_path).reset()
        count += 1
    _reset_current_weapon_record()
    logging.info(f"{LOG_PREFIX} {count} other character record(s) set aside")


mod = build_mod(
    settings_file=SETTINGS_FILE,
    options=[
        logging_enabled,
        show_readout,
        open_log_folder,
        web_enabled,
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
        readout_show_best_column,
        readout_show_crit_and_kills,
        readout_hidden_rows,
        web_host,
        web_port,
    ],
    keybinds=[],
    hooks=[],
    commands=[dmgweapons_command, dmgreport_command, dmgstatus_command, dmgreset_command, dmgweb_command],
    on_enable=on_enable,
    on_disable=on_disable,
)
