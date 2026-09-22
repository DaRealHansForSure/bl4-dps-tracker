"""
The hot path: once per frame, find out which damage instances are new, and whose they are.

BL4 applies damage natively and no function fires per hit, but the local player controller
keeps a small ring of `RecentPlayerCausedDamageInstance` structs (the source of the HUD's
floating numbers), and every write bumps that slot's RepDirtyFlag. Comparing the six flags
with the previous frame's values catches every instance (see poc/findings.md, section B).

Attribution, all of it from recorded sessions and probes (findings, section B):
  1. healing is not damage and is dropped before it reaches anything;
  2. status ticks (burn, radiation, ...) go to the last hit on that target;
  3. a hit with the gun flag (0x400, bullet impacts and sticky detonations) goes to the gun
     in hand, unless the swap rule applies: within a short grace after a weapon swap, on a
     target the previous gun hit in the last seconds, before the new gun has hit that
     target, the hit is the previous gun's delayed detonation;
  4. an unflagged hit on a target that took a flagged hit in the same frame is the
     elemental part of that bullet: gun in hand;
  5. an unflagged hit within a few frames of a projectile explosion whose owner is a weapon
     (the mod reports those from a hook on projectile:Explode) belongs to that weapon;
     an explosion owned by the character is a grenade;
  6. a target that is already being hit by the grenade or the skill keeps that source while
     the hits keep coming, so a stream never changes owner in the middle;
  7. otherwise the melee window (one second after a knife swing starts; the swing's own
     trick lasts about 0.6 s) claims it, then the action skill (from its trick beginning
     until that trick ends: an 18-second skill owns its damage for all 18 seconds), then
     the skill window that ambiguous signals open, then the grenade window (after a throw);
  8. anything left is Other, held back for a few frames first so a late explosion event can
     still claim it.

Every event carries the rule that claimed it (`why`, the RULE_* values below), so a hit that
looks wrong can be explained from the log alone instead of from a probe run.

The controller is fetched fresh on every tick and never reused across frames: a cached
wrapper points at freed memory once its world is torn down, and reading through it crashed
the game during a character switch (access violation, 2026-09-20).

This module never imports the SDK: the controller is obtained through a callable and only
its attributes are read, so the whole path can be exercised with fakes in the tests.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .attribution import (
    GRENADE_SOURCE,
    MELEE_SOURCE,
    OTHER_SOURCE,
    SKILL_SOURCE,
    SOURCE_GRENADE,
    SOURCE_WEAPON,
    UNKNOWN_SOURCE,
    DamageSource,
    WeaponResolver,
)

ERRORS_TO_REMEMBER = 3
# Outside the world (main menu, loading) there is nothing to poll; look again once a second.
IDLE_CHECK_INTERVAL_S = 1.0

# ERecentPlayerDamageFlags values are bit indexes, not masks: CriticalHit=1 -> 0x2, Healing=2 -> 0x4.
# Bits 10 and 14 are not in the reflected enum; their meaning was established from recorded
# sessions: 0x400 is set on bullet impacts and sticky detonations and on nothing else,
# 0x4000 is set on exactly the hit that kills the target.
CRIT_FLAG = 1 << 1
HEALING_FLAG = 1 << 2
WEAPON_FLAG = 1 << 10
KILL_FLAG = 1 << 14

KIND_DIRECT = "direct"
KIND_DOT = "dot"
KIND_HEAL = "heal"

# Which rule gave a hit its source, written to every event as "why".
RULE_HEAL = "heal"
RULE_STATUS = "status"
RULE_GUN_FLAG = "gun-flag"
RULE_SWAP_STICKY = "swap-sticky"
RULE_NO_GUN_IN_HAND = "no-gun-in-hand"
RULE_SAME_FRAME = "same-frame"
RULE_EXPLOSION = "explosion"
RULE_EXPLOSION_LATE = "explosion-late"
RULE_CONTINUED = "continued"
RULE_MELEE_WINDOW = "melee-window"
RULE_SKILL_RUNNING = "skill-running"
RULE_BLAST_WINDOW = "blast-window"
RULE_SAME_STREAM = "same-stream"
RULE_SKILL_WINDOW = "skill-window"
RULE_GRENADE_WINDOW = "grenade-window"
RULE_NONE = "none"

# Status effect ticks carry no reference to what applied them. They are credited to the last
# non-status hit on the same target inside this window; the game's own damage type names end
# in "_Status" for them, and not all of them have bIsDoT set.
STATUS_TYPE_SUFFIX = "_Status"
STATUS_ATTRIBUTION_WINDOW_S = 30.0
MAX_REMEMBERED_TARGETS = 128

# An explosion event and its damage land within about 50 ms of each other, in either order.
EXPLOSION_MATCH_WINDOW_S = 0.25
PENDING_HOLD_S = 0.1
MAX_REMEMBERED_EXPLOSIONS = 32
# Windows after a throw (drone gadgets keep firing for a while), after an explosion owned by
# the character, and after a knife swing (the ATrick_Melee_Hit trick lasts about 0.6 s and
# the damage lands inside it; the window is short so it takes little from a running skill).
GRENADE_WINDOW_S = 12.0
EXPLOSION_WINDOW_S = 3.0
# How long a blast that really happened outranks the soft skill window. The blast's own
# damage lands within a few frames; longer than that and it steals the next burst of an
# action skill, which is what a 3 s window did to the Exo Soldier's cannons on
# 2026-09-22 at 19:13:15 (two cannon hits read as the UAV grenade).
BLAST_WINDOW_S = 0.6
MELEE_WINDOW_S = 1.0
# When the swing trick ends, the melee window closes a few frames later: skill hits landing
# in the same second as a knife swing are otherwise indistinguishable from the knife's hit.
MELEE_END_GRACE_S = 0.15
# An action skill chains tricks and script actions with gaps of up to about 1.5 s while it
# runs; each trick start or end and each script action keeps the window open this much longer.
SKILL_WINDOW_S = 3.0
# A running skill keeps hitting long after its last script action (the Rogue's tokens attack
# for six seconds after the dice are thrown), so every hit credited to the skill keeps the
# window open this much longer: the window follows the skill's own damage and closes once
# the hits stop.
SKILL_HIT_GRACE_S = 1.5
# An action skill that never reports its end (cancelled, interrupted, a hook that did not
# fire) may not own damage forever: the running state expires after this, which is longer
# than any action skill in the game.
SKILL_MAX_RUN_S = 60.0
# That grace may not carry the window further than this past the last real skill signal.
# Without the cap one wrong attribution feeds itself: a grenade's area ticking three times
# a second was credited to the skill, and each of those hits pushed the window out again,
# so the stream never came back (session of 2026-09-22, 18:30:51 on).
SKILL_GRACE_CAP_S = 30.0
# An area (a corrosive cloud, a singularity, a skill's field) keeps hitting the same target
# tick after tick, long after the window that explains it has closed, and the windows are
# checked in a fixed order, so without this rule the stream would change owner in the middle
# and split one source's damage over two rows. A target whose last direct hit came from the
# grenade or the skill no longer ago than this keeps that source. Gun hits never take this
# path: they carry the gun flag and are decided before it. The span is shorter than the
# skill window's own grace on purpose: an area ticks several times a second, so a gap of a
# whole second means it stopped and the next hit is decided afresh.
STREAM_CONTINUATION_S = 1.0
# The sources whose streams are continued this way; melee is left out because its window is
# a single swing, and "other" because continuing a guess is not worth anything.
CONTINUED_KINDS = (SOURCE_GRENADE, SKILL_SOURCE.kind)
# Hits that only the windows explain are one stream while they keep coming this closely,
# whatever target each one lands on. Without this a single source changes label in the
# middle as a window lapses: the Exo Soldier's cannons read Skill from 19:27:58 and Other
# from 19:28:07 on 2026-09-22, seven identical pairs a second throughout.
# An action skill fires in bursts with gaps while the player looks for the next target: the
# cannons paused 3.7 s in the middle of one burst on 2026-09-22 (19:28:03.98 to 19:28:07.70)
# and the rest of the burst read Other. This rule only ever competes with Other, so the span
# is generous.
SOFT_STREAM_GAP_S = 5.0
# ... and a stream carried that way may not outlast the longest action skill.
SOFT_STREAM_MAX_S = 20.0

# The swap rule: a sticky detonates when its gun is swapped away, and the ring shows the hit
# with the new gun already in hand, within about 0.3 s of the swap being observed. The grace
# stays short so the new gun's own first shot is not taken from it.
SWAP_GRACE_S = 0.75
STICK_WINDOW_S = 10.0

EventSink = Callable[[list[dict[str, Any]]], None]
ControllerResolver = Callable[[], Any]


class DamagePoller:
    """Diffs the controller's damage ring every frame and hands new events to a sink.

    Counters (ticks, events, errors, overflow_frames, ignored_events) are plain attributes
    for the status line. `broken_reason` is set when the ring property is missing, which is
    what a game patch would look like; polling then stops for good instead of failing every
    frame. The note_* methods are fed by the mod's hooks and by its refresh loop.
    """

    def __init__(
        self,
        resolve_controller: ControllerResolver,
        ring_property: str,
        ring_size: int,
        resolver: WeaponResolver,
        sink: EventSink,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._resolve_controller = resolve_controller
        self._ring_property = ring_property
        self._ring_size = ring_size
        self._resolver = resolver
        self._sink = sink
        self._clock = clock
        self.controller: Any = None
        self.ticks = 0
        self.events = 0
        self.errors = 0
        self.overflow_frames = 0
        self.ignored_events = 0
        self.ignored_heals = 0
        self.recent_errors: list[str] = []
        self.broken_reason = ""
        # Called with the new controller when one is adopted, and with None when it is lost.
        self.on_controller_changed: Callable[[Any], None] | None = None
        self._dirty_flags: list[int] = []
        # The slot the game wrote last: the ring is filled circularly, so the slots that
        # changed in one frame are read starting after it, in the order the hits happened.
        self._last_written_slot = -1
        self._controller_address = 0
        self._idle_until = 0.0
        self._last_hit_by_target: dict[str, tuple[DamageSource, float]] = {}
        self._explosions: deque[tuple[float, DamageSource]] = deque()
        self._pending: list[tuple[float, dict[str, Any]]] = []
        self._grenade_until = 0.0
        # Until when a grenade that actually exploded explains an unattributed hit. The
        # throw window is a guess and yields to the skill; a blast does not.
        self._blast_until = 0.0
        # The grenade as a damage source: GRENADE_SOURCE until the equipped grenade's item
        # id is known, then the same with that id, so its hits can be named.
        self.grenade_source = GRENADE_SOURCE
        self._melee_until = 0.0
        self._skill_until = 0.0
        # The action skill tricks that have begun and not ended yet, by class name and start
        # time: while one runs, its damage is the skill's, however long it lasts.
        self._skill_tricks: dict[str, float] = {}
        # Whether an action skill trick may be taken for the player's own. It carries no
        # owner, so in a session with other players it says nothing (see note_skill_started).
        self.trust_skill_tricks = True
        # The last hit that only a window explained, and when: the rest of that stream keeps
        # its answer instead of following whichever window happens to be open.
        self._soft_source: DamageSource | None = None
        self._soft_at = 0.0
        self._soft_since = 0.0
        # When a signal that really is the player's action skill last arrived; the hit grace
        # may not push the skill window further than SKILL_MAX_RUN_S past it.
        self._skill_signal_at = 0.0
        self._last_grenade_action = 0.0
        self._last_melee_action = 0.0
        self._current_held: DamageSource | None = None
        self._previous_held: DamageSource | None = None
        self._swapped_at = 0.0
        self._hits_since_swap: set[str] = set()

    # ------------------------------------------------------------------ notes from the mod

    def note_explosion(self, now: float, source: DamageSource) -> None:
        """A projectile owned by `source` (a weapon, or the grenade) just exploded."""
        self._explosions.append((now, source))
        if len(self._explosions) > MAX_REMEMBERED_EXPLOSIONS:
            self._explosions.popleft()
        if source.kind != SOURCE_WEAPON:
            self._grenade_until = max(self._grenade_until, now + EXPLOSION_WINDOW_S)
            self._blast_until = max(self._blast_until, now + BLAST_WINDOW_S)
            self._last_grenade_action = now
        for _deadline, event in self._pending:
            if event["src"] == OTHER_SOURCE.kind and abs(now - float(event["t"])) <= EXPLOSION_MATCH_WINDOW_S:
                self._assign(event, source, RULE_EXPLOSION_LATE)

    def note_grenade_thrown(self, now: float) -> None:
        self._grenade_until = max(self._grenade_until, now + GRENADE_WINDOW_S)
        self._last_grenade_action = now

    def note_grenade_exploded(self, now: float) -> None:
        self.note_explosion(now, self.grenade_source)

    def set_grenade(self, item_id: str) -> None:
        """The equipped grenade's item id: its hits carry it, so the dashboard can name
        the grenade like a gun."""
        if item_id != self.grenade_source.item_id:
            self.grenade_source = DamageSource(GRENADE_SOURCE.kind, GRENADE_SOURCE.label, GRENADE_SOURCE.body, item_id)

    def note_melee_swing(self, now: float) -> None:
        self._melee_until = now + MELEE_WINDOW_S
        self._last_melee_action = now

    def note_melee_end(self, now: float) -> None:
        """The swing trick ended; its hit has landed by now (or will within a few frames)."""
        self._melee_until = min(self._melee_until, now + MELEE_END_GRACE_S)

    def note_skill_activity(self, now: float) -> None:
        """A skill script started an action: something of the skill happened just now. The
        signal says nothing about how long it lasts, so it opens the short window."""
        self._skill_signal_at = max(self._skill_signal_at, now)
        self._skill_until = max(self._skill_until, now + SKILL_WINDOW_S)

    def note_skill_started(self, now: float, trick: str = "") -> None:
        """An action skill trick began: the skill runs until that trick ends. The Exo
        Soldier's cannons fire for 18 s, far longer than any window, and everything they
        hit in that time is the skill's. Ignored while the tricks are not trusted (co-op,
        where the trick names no owner and a team mate's skill fires the same hook)."""
        self._skill_signal_at = max(self._skill_signal_at, now)
        if self.trust_skill_tricks:
            self._skill_tricks[trick] = now

    def skill_state(self) -> str:
        """What the mod would say about the action skill right now, for dmgstatus."""
        if not self.trust_skill_tricks:
            return "tricks ignored (no owner in a shared session)"
        return f"{len(self._skill_tricks)} trick(s) running"

    def note_skill_ended(self, now: float, trick: str = "") -> None:
        """That trick ended: the skill stops owning damage, after a short grace for what is
        still in flight."""
        self._skill_signal_at = max(self._skill_signal_at, now)
        if self._skill_tricks.pop(trick, None) is not None or not self._skill_tricks:
            self._skill_tricks.clear()
            self._skill_until = max(self._skill_until, now + SKILL_WINDOW_S)

    def note_skill_token(self, now: float) -> None:
        """A skill token was added to a stack. The call is static and carries no owner, so
        it does not say whose skill it was: it keeps a skill that is already running open
        and never starts one. Before that rule, tokens from anywhere opened the window and
        took a burning grenade's damage with them."""
        if now < self._skill_until:
            self.note_skill_activity(now)

    def note_weapon_in_hand(self, now: float, held: DamageSource) -> None:
        """Called by the refresh loop; a change of the held weapon starts the swap grace."""
        current = self._current_held
        if current is not None and held.item_id == current.item_id:
            return
        if current is not None and current.item_id != "":
            self._previous_held = current
            self._swapped_at = now
            self._hits_since_swap = set()
        self._current_held = held

    # ------------------------------------------------------------------ per frame

    def tick(self) -> None:
        """Called once per frame from the tick hook. Never raises."""
        self.ticks += 1
        if self.broken_reason:
            return
        now = self._clock()
        new_events: list[dict[str, Any]] = []
        if now >= self._idle_until:
            try:
                self._refresh_controller()
                if self.controller is None:
                    self._idle_until = now + IDLE_CHECK_INTERVAL_S
                else:
                    new_events = self._collect_new_events(now)
            except Exception as ex:  # noqa: BLE001 - nothing may escape into the game thread
                self._note_error(ex)
                self._drop_controller()
                new_events = []
        ready = self._release_pending(now)
        if new_events or ready:
            batch = sorted(new_events + ready, key=lambda event: float(event["t"]))
            self.events += len(batch)
            self._sink(batch)

    def _release_pending(self, now: float) -> list[dict[str, Any]]:
        if not self._pending:
            return []
        ready = [event for deadline, event in self._pending if deadline <= now]
        if ready:
            self._pending = [(deadline, event) for deadline, event in self._pending if deadline > now]
        return ready

    # ------------------------------------------------------------------ controller

    def _refresh_controller(self) -> None:
        """Take this frame's controller wrapper; adopt it as a new controller when its address
        differs from the last one (map load, character switch), drop it when there is none."""
        controller = self._resolve_controller()
        if controller is None:
            self._drop_controller()
            return
        address = int(controller._get_address())
        if address == self._controller_address:
            self.controller = controller
            return
        ring = getattr(controller, self._ring_property, None)
        if ring is None:
            # The main menu has a controller without a pawn and without the ring; that is not
            # a broken game, just not in the world yet. With a pawn present the ring must exist.
            if controller.Pawn is not None:
                self.broken_reason = (
                    f"property {self._ring_property} is missing on {controller.Class.Name}; "
                    "the game changed, check the ring_property setting"
                )
            self._drop_controller()
            return
        # Baseline the flags so instances from before the resolve are not replayed as new hits.
        self._dirty_flags = [int(ring[index].RepDirtyFlag) for index in range(self._ring_size)]
        self._last_written_slot = self._ring_size - 1
        self.controller = controller
        self._controller_address = address
        if self.on_controller_changed is not None:
            self.on_controller_changed(controller)

    def _drop_controller(self) -> None:
        had_controller = self._controller_address != 0
        self.controller = None
        self._controller_address = 0
        if had_controller and self.on_controller_changed is not None:
            self.on_controller_changed(None)

    # ------------------------------------------------------------------ ring diff

    def _collect_new_events(self, now: float) -> list[dict[str, Any]]:
        ring = getattr(self.controller, self._ring_property)
        pawn = self.controller.Pawn
        # Damage dealt from a vehicle is not the player's gun play; it is dropped, but the
        # flags still advance so those hits are not replayed once the player is on foot.
        tracking = self._resolver.is_character(pawn)
        changed_slots: list[int] = []
        for index in range(self._ring_size):
            dirty = int(ring[index].RepDirtyFlag)
            if dirty == self._dirty_flags[index]:
                continue
            self._dirty_flags[index] = dirty
            changed_slots.append(index)
        changed = [ring[index] for index in self._in_write_order(changed_slots)]
        if len(changed) == self._ring_size:
            # Every slot changed within one frame: the ring may have wrapped and dropped hits.
            self.overflow_frames += 1
        if not tracking:
            self.ignored_events += len(changed)
            return []
        # Targets that took a bullet impact this frame: their unflagged hits in the same frame
        # are the elemental part of that bullet, not a grenade or a swing.
        flagged_targets: set[str] = set()
        for instance in changed:
            if int(instance.DamageFlags.Flags) & WEAPON_FLAG:
                target = instance.DamagedActor
                if target is not None:
                    flagged_targets.add(str(target.Name))
        ready_now: list[dict[str, Any]] = []
        for instance in changed:
            event = self._read_instance(instance, pawn, now, flagged_targets)
            if event["kind"] == KIND_HEAL:
                self.ignored_heals += 1
                continue
            if self._should_hold(event):
                self._pending.append((now + PENDING_HOLD_S, event))
            else:
                ready_now.append(event)
        return ready_now

    def _in_write_order(self, slots: list[int]) -> list[int]:
        """The changed slots in the order the game filled them: continuing around the ring
        from the slot after the last one written."""
        if not slots:
            return slots
        start = (self._last_written_slot + 1) % self._ring_size
        ordered = sorted(slots, key=lambda slot: (slot - start) % self._ring_size)
        self._last_written_slot = ordered[-1]
        return ordered

    @staticmethod
    def _should_hold(event: dict[str, Any]) -> bool:
        return event["kind"] == KIND_DIRECT and event["src"] == OTHER_SOURCE.kind

    def _read_instance(self, instance: Any, pawn: Any, now: float, flagged_targets: set[str]) -> dict[str, Any]:
        flags = int(instance.DamageFlags.Flags)
        damage_type = instance.DamageType
        target = instance.DamagedActor
        target_name = str(target.Name) if target is not None else ""
        # Some instances (grenades, cleared slots) carry an unresolved damage type handle whose
        # fields raise on access; those hits are still damage, so read the fields with defaults.
        type_name = str(getattr(damage_type, "_name", "None"))
        is_heal = bool(flags & HEALING_FLAG) or bool(getattr(damage_type, "bIsHealingType", False))
        is_status = bool(getattr(damage_type, "bIsDoT", False)) or type_name.endswith(STATUS_TYPE_SUFFIX)
        if is_heal:
            kind = KIND_HEAL
            source, why = OTHER_SOURCE, RULE_HEAL
        elif is_status:
            kind = KIND_DOT
            source, why = self._status_source(target_name, now), RULE_STATUS
        else:
            kind = KIND_DIRECT
            source, why = self._direct_source(flags, target_name, flagged_targets, pawn, now)
            self._remember_hit(target_name, source, now)
            # Every hit of a source that has no flag of its own belongs to the running
            # stream, however it was decided: that is what keeps a burst on one label when
            # the window that explained its first hits lapses in the middle.
            if source.kind in CONTINUED_KINDS:
                self._remember_soft(source, now)
            if source is SKILL_SOURCE:
                self._extend_skill_window(now)
        event = {
            "t": now,
            "dmg": float(instance.TotalDamage),
            "kind": kind,
            "crit": bool(flags & CRIT_FLAG),
            "kill": bool(flags & KILL_FLAG),
            "elem": int(getattr(damage_type, "ElementalType", 0) or 0),
            "flags": flags,
            "dtype": type_name,
            "target": target_name,
        }
        self._assign(event, source, why)
        return event

    @staticmethod
    def _assign(event: dict[str, Any], source: DamageSource, why: str) -> None:
        event["src"] = source.kind
        event["weapon"] = source.label
        event["body"] = source.body
        event["wid"] = source.item_id
        event["why"] = why

    # ------------------------------------------------------------------ attribution

    def _direct_source(self, flags: int, target_name: str, flagged_targets: set[str], pawn: Any, now: float) -> tuple[DamageSource, str]:
        """The source of a direct hit and the rule that decided it."""
        if flags & WEAPON_FLAG:
            return self._flagged_source(target_name, pawn, now)
        if target_name in flagged_targets:
            return self._resolver.weapon_in_hand(pawn), RULE_SAME_FRAME
        exploded_by = self._recent_explosion_source(now)
        if exploded_by is not None:
            return exploded_by, RULE_EXPLOSION
        continued = self._continued_stream(target_name, now)
        if continued is not None:
            return continued, RULE_CONTINUED
        if now < self._melee_until:
            return MELEE_SOURCE, RULE_MELEE_WINDOW
        # An action skill that is really running owns what it hits.
        if self._skill_running(now):
            return self._remember_soft(SKILL_SOURCE, now), RULE_SKILL_RUNNING
        # Then the grenade's own facts: the throw is a call on the player's pawn and the
        # explosion carries its instigator. The skill window below is not a fact at all,
        # because passive skill scripts open it several times a second through a whole
        # fight; on 2026-09-22 it took every grenade hit of a co-op fight, kills included.
        if now < self._blast_until:
            return self.grenade_source, RULE_BLAST_WINDOW
        if now < self._grenade_until:
            return self._remember_soft(self.grenade_source, now), RULE_GRENADE_WINDOW
        if now < self._skill_until:
            return self._remember_soft(SKILL_SOURCE, now), RULE_SKILL_WINDOW
        # No window explains this one. If the same stream of unexplained hits was running a
        # moment ago, it is still that source: a window lapsing in the middle must not
        # rename what is plainly one burst.
        carried = self._carried_soft_stream(now)
        if carried is not None:
            return carried, RULE_SAME_STREAM
        self._soft_source = None
        return OTHER_SOURCE, RULE_NONE

    def _flagged_source(self, target_name: str, pawn: Any, now: float) -> tuple[DamageSource, str]:
        """The gun in hand, or the previous gun for a sticky that went off on the swap."""
        held = self._resolver.weapon_in_hand(pawn)
        previous = self._previous_held
        within_grace = now - self._swapped_at <= SWAP_GRACE_S
        if previous is not None and within_grace and target_name and target_name not in self._hits_since_swap:
            remembered = self._last_hit_by_target.get(target_name)
            if remembered is not None:
                last_source, when = remembered
                if last_source.item_id == previous.item_id and now - when <= STICK_WINDOW_S:
                    return previous, RULE_SWAP_STICKY
        if target_name:
            self._hits_since_swap.add(target_name)
        if held.item_id == "":
            return self._gun_without_one_in_hand(target_name, now), RULE_NO_GUN_IN_HAND
        return held, RULE_GUN_FLAG

    def _gun_without_one_in_hand(self, target_name: str, now: float) -> DamageSource:
        """Gun damage while no gun resolves (mid swap, the slot empty for a moment): a sticky
        or an explosive round of a gun that is no longer in hand. The gun that last hit this
        target within the stick window, else the gun swapped away, else unknown."""
        remembered = self._last_hit_by_target.get(target_name) if target_name else None
        if remembered is not None:
            last_source, when = remembered
            if last_source.kind == SOURCE_WEAPON and now - when <= STICK_WINDOW_S:
                return last_source
        if self._previous_held is not None and self._previous_held.item_id != "":
            return self._previous_held
        return UNKNOWN_SOURCE

    def _remember_soft(self, source: DamageSource, now: float) -> DamageSource:
        """Start (or continue) the stream of hits that only a window explains."""
        if self._soft_source is None or self._soft_source.kind != source.kind or now - self._soft_at > SOFT_STREAM_GAP_S:
            self._soft_since = now
        self._soft_source = source
        self._soft_at = now
        return source

    def _carried_soft_stream(self, now: float) -> DamageSource | None:
        """The source of the stream of window-explained hits that is still running, if the
        last one is barely older than this one and the stream has not run longer than a
        skill could. A stream that outlives that is a guess feeding itself."""
        if self._soft_source is None:
            return None
        if now - self._soft_at > SOFT_STREAM_GAP_S or now - self._soft_since > SOFT_STREAM_MAX_S:
            return None
        self._soft_at = now
        return self._soft_source

    def _skill_running(self, now: float) -> bool:
        """Whether an action skill trick is running. A trick whose end never arrived is
        dropped after SKILL_MAX_RUN_S so a missed signal cannot own the rest of the session."""
        for trick, started in list(self._skill_tricks.items()):
            if now - started > SKILL_MAX_RUN_S:
                del self._skill_tricks[trick]
        return bool(self._skill_tricks)

    def _extend_skill_window(self, now: float) -> None:
        """A hit credited to the skill keeps the window open, but never further than
        SKILL_MAX_RUN_S past the last real skill signal, so the window cannot live on its
        own attributions."""
        limit = self._skill_signal_at + SKILL_GRACE_CAP_S
        self._skill_until = max(self._skill_until, min(now + SKILL_HIT_GRACE_S, limit))

    def _continued_stream(self, target_name: str, now: float) -> DamageSource | None:
        """The grenade or the skill is already hitting this target and its last hit is
        barely a moment old: this one continues that stream, whatever window opened or
        closed meanwhile. The grenade comes back as the current grenade, whose item id may
        have been learnt since."""
        remembered = self._last_hit_by_target.get(target_name)
        if remembered is None:
            return None
        source, when = remembered
        if source.kind not in CONTINUED_KINDS or now - when > STREAM_CONTINUATION_S:
            return None
        return self.grenade_source if source.kind == SOURCE_GRENADE else source

    def _recent_explosion_source(self, now: float) -> DamageSource | None:
        for when, source in reversed(self._explosions):
            if abs(now - when) <= EXPLOSION_MATCH_WINDOW_S:
                return source
            if now - when > EXPLOSION_MATCH_WINDOW_S:
                return None
        return None

    def _remember_hit(self, target_name: str, source: DamageSource, now: float) -> None:
        if not target_name:
            return
        if len(self._last_hit_by_target) >= MAX_REMEMBERED_TARGETS:
            cutoff = now - STATUS_ATTRIBUTION_WINDOW_S
            stale = [name for name, (_, when) in self._last_hit_by_target.items() if when < cutoff]
            for name in stale:
                del self._last_hit_by_target[name]
            if len(self._last_hit_by_target) >= MAX_REMEMBERED_TARGETS:
                self._last_hit_by_target.clear()
        self._last_hit_by_target[target_name] = (source, now)

    def _status_source(self, target_name: str, now: float) -> DamageSource:
        """The source of the last non-status hit on this target, if recent enough."""
        remembered = self._last_hit_by_target.get(target_name)
        if remembered is None:
            return OTHER_SOURCE
        source, when = remembered
        if now - when > STATUS_ATTRIBUTION_WINDOW_S:
            return OTHER_SOURCE
        return source

    # ------------------------------------------------------------------ errors

    def _note_error(self, ex: Exception) -> None:
        self.errors += 1
        if len(self.recent_errors) < ERRORS_TO_REMEMBER:
            self.recent_errors.append(f"{type(ex).__name__}: {ex}")
