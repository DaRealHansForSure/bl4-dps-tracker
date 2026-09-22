"""The ring-buffer poller and its attribution rules, against a fake player controller."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from dmgtracker.attribution import GRENADE_SOURCE, MELEE_SOURCE, DamageSource, WeaponResolver
from dmgtracker.poller import (
    CRIT_FLAG,
    STREAM_CONTINUATION_S,
    HEALING_FLAG,
    KILL_FLAG,
    PENDING_HOLD_S,
    SKILL_HIT_GRACE_S,
    SKILL_GRACE_CAP_S,
    SKILL_MAX_RUN_S,
    SKILL_WINDOW_S,
    SWAP_GRACE_S,
    WEAPON_FLAG,
    DamagePoller,
)

RING_PROPERTY = "RecentlyCausedDamageInstances"
RING_SIZE = 6
SNIPER_PATH = "/Game/Body_JAK_SR.Body_JAK_SR"
TORGUE_PATH = "/Game/Body_TOR_SG.Body_TOR_SG"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def fake_instance(damage: float = 0.0, flags: int = 0, is_dot: bool = False, is_heal_type: bool = False,
                  target: str | None = "Char_CatAdult_1", dtype: str = "Normal") -> SimpleNamespace:
    damaged_actor = None if target is None else SimpleNamespace(Name=target)
    return SimpleNamespace(
        RepDirtyFlag=0,
        TotalDamage=damage,
        DamageFlags=SimpleNamespace(Flags=flags),
        DamageType=SimpleNamespace(bIsDoT=is_dot, bIsHealingType=is_heal_type, ElementalType=0, _name=dtype),
        DamagedActor=damaged_actor,
    )


def fake_weapon(instance_id: int, body_path: str) -> SimpleNamespace:
    return SimpleNamespace(
        Item=SimpleNamespace(data=SimpleNamespace(InstanceId=instance_id)),
        BodyData=SimpleNamespace(_path_name=lambda: body_path),
        Class=SimpleNamespace(Name="OakWeapon"),
    )


def fake_pawn(weapon: SimpleNamespace | None) -> SimpleNamespace:
    slots = [] if weapon is None else [SimpleNamespace(Weapon=weapon)]
    return SimpleNamespace(
        Class=SimpleNamespace(Name="OakCharacter"),
        ActiveWeapons=SimpleNamespace(Slots=slots),
        EquippedInventorySlots=SimpleNamespace(items=[]),
    )


def fake_controller(name: str = "OakPlayerController_1", with_ring: bool = True, with_pawn: bool = True,
                    address: int = 0x1000):
    pawn = fake_pawn(fake_weapon(277, SNIPER_PATH)) if with_pawn else None
    controller = SimpleNamespace(
        Pawn=pawn,
        Class=SimpleNamespace(Name="OakPlayerController"),
        _path_name=lambda: f"/Game/Maps/World_P:PersistentLevel.{name}",
        _get_address=lambda: address,
    )
    if with_ring:
        setattr(controller, RING_PROPERTY, [fake_instance() for _ in range(RING_SIZE)])
    return controller


def write_hit(controller, slot: int, damage: float, flags: int = 0, **kwargs) -> None:
    """Simulate the game writing a damage instance into one ring slot."""
    ring = getattr(controller, RING_PROPERTY)
    previous_dirty = ring[slot].RepDirtyFlag
    ring[slot] = fake_instance(damage, flags, **kwargs)
    ring[slot].RepDirtyFlag = (previous_dirty + 1) % 256


class DamagePollerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.controller = fake_controller()
        self.current_controller = self.controller
        self.received: list[dict] = []
        self.resolver = WeaponResolver()
        self.poller = DamagePoller(
            resolve_controller=lambda: self.current_controller,
            ring_property=RING_PROPERTY,
            ring_size=RING_SIZE,
            resolver=self.resolver,
            sink=self.received.extend,
            clock=self.clock,
        )

    def settle(self) -> None:
        """Let held-back events (unexplained unflagged hits) reach the sink."""
        self.clock.advance(PENDING_HOLD_S + 0.05)
        self.poller.tick()

    def hold(self, weapon: SimpleNamespace | None) -> None:
        """Put a weapon in the pawn's hand and tell the poller, as the refresh loop would."""
        self.controller.Pawn = fake_pawn(weapon)
        held = self.resolver.weapon_in_hand(self.controller.Pawn)
        self.poller.note_weapon_in_hand(self.clock.now, held)

    # ------------------------------------------------------------------ basics

    def test_first_tick_adopts_the_controller_without_replaying_old_hits(self) -> None:
        write_hit(self.controller, 0, 100.0)
        self.poller.tick()
        self.settle()
        self.assertIs(self.poller.controller, self.controller)
        self.assertEqual(self.received, [])

    def test_new_hits_become_events(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 1, 48.04, flags=CRIT_FLAG | WEAPON_FLAG, target="Char_CatYoung_9", dtype="Corrosive")
        self.poller.tick()
        self.assertEqual(len(self.received), 1)
        event = self.received[0]
        self.assertEqual(event["dmg"], 48.04)
        self.assertTrue(event["crit"])
        self.assertFalse(event["kill"])
        self.assertEqual(event["kind"], "direct")
        self.assertEqual(event["src"], "weapon")
        self.assertEqual(event["weapon"], "Jakobs Sniper")
        self.assertEqual(event["wid"], "instance:277")
        self.assertEqual(event["target"], "Char_CatYoung_9")
        self.assertEqual(event["dtype"], "Corrosive")
        self.assertEqual(event["t"], self.clock.now)
        self.assertEqual(self.poller.events, 1)

    def test_unflagged_hits_are_other_by_default_after_the_hold(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 300.9, flags=KILL_FLAG, target="Char_A_1")
        write_hit(self.controller, 1, 300.9, flags=0, target="Char_B_2")
        self.poller.tick()
        self.assertEqual(self.received, [], "unexplained hits are held back for a late explosion event")
        self.settle()
        first, second = self.received
        self.assertEqual((first["src"], first["weapon"], first["wid"]), ("other", "Other", ""))
        self.assertTrue(first["kill"])
        self.assertEqual(second["src"], "other")
        self.assertFalse(second["kill"])

    def test_healing_is_dropped_entirely(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 30.0, flags=WEAPON_FLAG | HEALING_FLAG)
        write_hit(self.controller, 1, 30.0, flags=WEAPON_FLAG, is_heal_type=True)
        write_hit(self.controller, 2, 5.0, flags=WEAPON_FLAG, is_dot=True)
        self.poller.tick()
        self.settle()
        self.assertEqual([event["kind"] for event in self.received], ["dot"])
        self.assertEqual(self.poller.ignored_heals, 2)

    def test_vehicle_hits_are_ignored_but_not_replayed(self) -> None:
        self.poller.tick()
        self.controller.Pawn = SimpleNamespace(Class=SimpleNamespace(Name="OakVehicle"))
        write_hit(self.controller, 0, 500.0, flags=WEAPON_FLAG)
        self.poller.tick()
        self.assertEqual(self.received, [])
        self.assertEqual(self.poller.ignored_events, 1)
        self.controller.Pawn = fake_controller().Pawn
        self.poller.tick()
        self.assertEqual(self.received, [], "the vehicle hit must not surface once on foot")
        write_hit(self.controller, 1, 20.0, flags=WEAPON_FLAG)
        self.poller.tick()
        self.assertEqual([event["dmg"] for event in self.received], [20.0])

    def test_unchanged_slots_produce_nothing(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 2, 10.0, flags=WEAPON_FLAG)
        self.poller.tick()
        self.poller.tick()
        self.poller.tick()
        self.assertEqual(len(self.received), 1)

    def test_unresolved_damage_type_handle_is_tolerated(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 250.0, flags=WEAPON_FLAG)
        # An unresolved FGameDataHandle raises AttributeError for every field except _name.
        getattr(self.controller, RING_PROPERTY)[0].DamageType = SimpleNamespace(_name="None")
        self.poller.tick()
        self.assertEqual(self.poller.errors, 0)
        event = self.received[0]
        self.assertEqual(event["dmg"], 250.0)
        self.assertEqual(event["kind"], "direct")
        self.assertEqual(event["elem"], 0)
        self.assertEqual(event["dtype"], "None")

    def test_target_gone_is_empty_string(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 5.0, flags=WEAPON_FLAG, target=None)
        self.poller.tick()
        self.assertEqual(self.received[0]["target"], "")

    def test_slots_changed_in_one_frame_are_read_in_write_order(self) -> None:
        self.poller.tick()
        for slot in range(4):
            write_hit(self.controller, slot, 10.0 + slot, flags=WEAPON_FLAG, target=f"Char_{slot}")
        self.poller.tick()
        self.assertEqual([e["dmg"] for e in self.received], [10.0, 11.0, 12.0, 13.0])
        # The next three hits wrap around: slots 4, 5 and 0, in that order.
        write_hit(self.controller, 4, 20.0, flags=WEAPON_FLAG, target="Char_4")
        write_hit(self.controller, 5, 21.0, flags=WEAPON_FLAG | KILL_FLAG, target="Char_5")
        write_hit(self.controller, 0, 22.0, flags=WEAPON_FLAG, target="Char_0")
        self.poller.tick()
        self.assertEqual([e["dmg"] for e in self.received[4:]], [20.0, 21.0, 22.0])

    def test_all_slots_changed_counts_as_overflow_suspect(self) -> None:
        self.poller.tick()
        for slot in range(RING_SIZE):
            write_hit(self.controller, slot, 1.0, flags=WEAPON_FLAG)
        self.poller.tick()
        self.assertEqual(self.poller.overflow_frames, 1)
        self.assertEqual(len(self.received), RING_SIZE)

    def test_read_error_is_counted_and_controller_dropped(self) -> None:
        self.poller.tick()
        getattr(self.controller, RING_PROPERTY)[0] = SimpleNamespace(RepDirtyFlag="not a number")
        self.poller.tick()
        self.assertEqual(self.poller.errors, 1)
        self.assertIsNone(self.poller.controller)
        self.assertEqual(len(self.poller.recent_errors), 1)

    # ------------------------------------------------------------------ controller changes

    def test_controller_change_is_adopted_at_once(self) -> None:
        self.poller.tick()
        replacement = fake_controller(name="OakPlayerController_2", address=0x2000)
        write_hit(replacement, 0, 7.0, flags=WEAPON_FLAG)
        self.current_controller = replacement
        self.poller.tick()
        self.assertIs(self.poller.controller, replacement)
        self.assertEqual(self.received, [], "the hit from before the adoption is baseline, not replayed")
        write_hit(replacement, 0, 8.0, flags=WEAPON_FLAG)
        self.poller.tick()
        self.assertEqual([event["dmg"] for event in self.received], [8.0])
        self.assertEqual(self.received[0]["weapon"], "Jakobs Sniper")

    def test_controller_change_callback_reports_adoption_and_loss(self) -> None:
        seen = []
        self.poller.on_controller_changed = seen.append
        self.poller.tick()
        self.poller.tick()
        self.assertEqual(seen, [self.controller])
        self.current_controller = None
        self.poller.tick()
        self.poller.tick()
        self.assertEqual(seen, [self.controller, None])
        self.current_controller = self.controller
        self.clock.advance(1.0)
        self.poller.tick()
        self.assertEqual(seen, [self.controller, None, self.controller])

    def test_same_controller_is_not_readopted_across_frames(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 3.0, flags=WEAPON_FLAG)
        # A fresh wrapper with the same address is the same controller; hits must not be lost.
        self.current_controller = fake_controller()
        setattr(self.current_controller, RING_PROPERTY, getattr(self.controller, RING_PROPERTY))
        self.poller.tick()
        self.assertEqual([event["dmg"] for event in self.received], [3.0])

    def test_no_controller_is_quiet_and_backs_off(self) -> None:
        calls = []
        self.current_controller = None
        self.poller._resolve_controller = lambda: calls.append(1)  # noqa: SLF001 - counting lookups
        self.poller.tick()
        self.poller.tick()
        self.poller.tick()
        self.assertEqual(len(calls), 1, "only one lookup per idle second")
        self.clock.advance(1.0)
        self.poller.tick()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.poller.controller)
        self.assertEqual(self.poller.errors, 0)

    def test_menu_controller_backs_off_too(self) -> None:
        self.current_controller = fake_controller(with_ring=False, with_pawn=False)
        self.poller.tick()
        first_ticks = self.poller.ticks
        self.poller.tick()
        self.assertEqual(self.poller.ticks, first_ticks + 1)
        self.assertIsNone(self.poller.controller)

    def test_ring_missing_in_the_world_marks_the_mechanism_broken(self) -> None:
        self.current_controller = fake_controller(with_ring=False, with_pawn=True)
        self.poller.tick()
        self.assertIn(RING_PROPERTY, self.poller.broken_reason)
        self.clock.advance(5.0)
        self.poller.tick()
        self.assertEqual(self.poller.errors, 0)

    # ------------------------------------------------------------------ status ticks

    def test_status_ticks_are_credited_to_the_last_hit_on_that_target(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 50.0, flags=WEAPON_FLAG, target="Char_Leader_7", dtype="Radiation")
        self.poller.note_grenade_thrown(self.clock.now)
        write_hit(self.controller, 1, 300.0, flags=0, target="Char_Psycho_3", dtype="Normal")
        self.poller.tick()
        write_hit(self.controller, 2, 6.3, flags=0x90, target="Char_Leader_7", dtype="Radiation_Status")
        write_hit(self.controller, 3, 6.3, flags=0x80, target="Char_Psycho_3", dtype="Radiation_Status", is_dot=True)
        write_hit(self.controller, 4, 6.3, flags=0x80, target="Char_Unknown_1", dtype="Radiation_Status")
        self.poller.tick()
        ticks = self.received[2:]
        self.assertEqual([event["kind"] for event in ticks], ["dot", "dot", "dot"])
        self.assertEqual([event["weapon"] for event in ticks], ["Jakobs Sniper", "Grenade", "Other"])
        self.assertEqual([event["src"] for event in ticks], ["weapon", "grenade", "other"])

    def test_status_attribution_expires(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 50.0, flags=WEAPON_FLAG, target="Char_Leader_7")
        self.poller.tick()
        self.clock.advance(31.0)
        write_hit(self.controller, 1, 6.3, flags=0x80, target="Char_Leader_7", dtype="Radiation_Status")
        self.poller.tick()
        self.assertEqual(self.received[-1]["weapon"], "Other")

    # ------------------------------------------------------------------ explosions

    def test_explosion_before_the_damage_claims_it(self) -> None:
        torgue = DamageSource("weapon", "Torgue Shotgun", "Body_TOR_SG", "seed281")
        self.poller.tick()
        self.poller.note_explosion(self.clock.now, torgue)
        self.clock.advance(0.04)
        write_hit(self.controller, 0, 902.8, flags=KILL_FLAG, target="Char_SoldierSMG")
        self.poller.tick()
        self.assertEqual((self.received[0]["weapon"], self.received[0]["wid"]), ("Torgue Shotgun", "seed281"))

    def test_explosion_after_the_damage_claims_the_held_event(self) -> None:
        torgue = DamageSource("weapon", "Torgue Shotgun", "Body_TOR_SG", "seed281")
        self.poller.tick()
        write_hit(self.controller, 0, 300.9, flags=0x10, target="Char_GruntSMG")
        self.poller.tick()
        self.assertEqual(self.received, [])
        self.clock.advance(0.05)
        self.poller.note_explosion(self.clock.now, torgue)
        self.settle()
        self.assertEqual([(e["weapon"], e["src"]) for e in self.received], [("Torgue Shotgun", "weapon")])

    def test_grenade_explosion_claims_and_opens_a_window(self) -> None:
        self.poller.tick()
        self.poller.note_grenade_exploded(self.clock.now)
        self.clock.advance(0.1)
        write_hit(self.controller, 0, 270.7, flags=0, target="Char_A_1")
        self.poller.tick()
        self.clock.advance(2.0)
        write_hit(self.controller, 1, 244.4, flags=0, target="Char_A_1")
        self.poller.tick()
        self.settle()
        self.assertEqual([e["src"] for e in self.received], ["grenade", "grenade"])

    def test_same_frame_elemental_hit_stays_with_the_gun_even_in_a_window(self) -> None:
        self.poller.tick()
        self.poller.note_grenade_thrown(self.clock.now)
        write_hit(self.controller, 0, 56.9, flags=WEAPON_FLAG, target="Char_A_1", dtype="Radiation")
        write_hit(self.controller, 1, 56.9, flags=0, target="Char_A_1", dtype="Radiation")
        self.poller.tick()
        self.assertEqual([e["weapon"] for e in self.received], ["Jakobs Sniper", "Jakobs Sniper"])

    # ------------------------------------------------------------------ grenade and melee windows

    def test_grenade_window_claims_unflagged_hits(self) -> None:
        self.poller.tick()
        self.poller.note_grenade_thrown(self.clock.now)
        self.clock.advance(2.0)
        write_hit(self.controller, 0, 333.0, flags=0, target="Char_A_1")
        write_hit(self.controller, 1, 333.0, flags=KILL_FLAG, target="Char_B_2")
        write_hit(self.controller, 2, 50.0, flags=WEAPON_FLAG, target="Char_C_3")
        self.poller.tick()
        self.assertEqual([e["weapon"] for e in self.received], [GRENADE_SOURCE.label, GRENADE_SOURCE.label, "Jakobs Sniper"])
        self.assertEqual(self.received[0]["src"], "grenade")
        self.clock.advance(11.0)
        write_hit(self.controller, 3, 300.9, flags=0, target="Char_D_4")
        self.poller.tick()
        self.settle()
        self.assertEqual(self.received[-1]["weapon"], "Other", "the window has passed")

    def test_melee_window_claims_unflagged_hits_briefly(self) -> None:
        self.poller.tick()
        self.poller.note_melee_swing(self.clock.now)
        self.clock.advance(0.5)
        write_hit(self.controller, 0, 270.7, flags=0, target="Char_A_1")
        self.poller.tick()
        self.assertEqual((self.received[-1]["src"], self.received[-1]["weapon"]), ("melee", MELEE_SOURCE.label))
        self.clock.advance(1.0)
        write_hit(self.controller, 1, 270.7, flags=0, target="Char_A_1")
        self.poller.tick()
        self.settle()
        self.assertEqual(self.received[-1]["src"], "other", "the swing window is over")

    def test_skill_window_claims_hits_while_the_skill_runs(self) -> None:
        self.poller.tick()
        self.poller.note_grenade_thrown(self.clock.now)
        self.poller.note_skill_activity(self.clock.now)
        self.clock.advance(1.0)
        write_hit(self.controller, 0, 24.3, flags=0, target="Char_A_1")
        write_hit(self.controller, 1, 9.1, flags=0, target="Char_A_1", dtype="Fire")
        self.poller.tick()
        self.assertEqual([e["src"] for e in self.received], ["skill", "skill"])
        self.assertEqual(self.received[0]["weapon"], "Skill")
        self.clock.advance(2.5)
        write_hit(self.controller, 2, 24.3, flags=0, target="Char_A_1")
        self.poller.tick()
        self.assertEqual(self.received[-1]["src"], "grenade", "the skill window closed, the throw window is still open")

    def test_skill_hits_keep_the_skill_window_open(self) -> None:
        """Tokens attack for seconds after the last script action: the window follows the hits."""
        self.poller.tick()
        self.poller.note_skill_activity(self.clock.now)
        for step in range(12):
            self.clock.advance(0.5)
            write_hit(self.controller, step % 6, 239.7, flags=0, target="Char_A_1")
            self.poller.tick()
        self.assertEqual({event["src"] for event in self.received}, {"skill"}, "hits up to 6 s after the action")
        self.clock.advance(2.0)
        write_hit(self.controller, 0, 239.7, flags=0, target="Char_A_1")
        self.poller.tick()
        self.settle()
        self.assertEqual(self.received[-1]["src"], "other", "1.5 s without a skill hit closes the window")

    def test_an_action_skill_owns_its_damage_until_its_trick_ends(self) -> None:
        """The Exo Soldier's cannons fire for 18 s with gaps between the bursts, and a
        grenade window is open the whole time (session of 2026-09-22, 18:58): without the
        skill being a state, the gaps fell to the grenade window and then to Other."""
        self.poller.tick()
        self.poller.note_grenade_thrown(self.clock.now)
        self.clock.advance(1.0)
        self.poller.note_skill_started(self.clock.now, "Trick_ActionSkill_Cannons_C")
        for gap, target in ((1.5, "Char_A_1"), (5.0, "Char_B_2"), (5.0, "Char_C_3"), (5.0, "Char_D_4")):
            self.clock.advance(gap)
            write_hit(self.controller, 0, 26.5, flags=0, target=target)
            write_hit(self.controller, 1, 9.9, flags=0, target=target, dtype="Fire")
            self.poller.tick()
            self.assertEqual([event["src"] for event in self.received[-2:]], ["skill", "skill"],
                             f"{gap} s into the skill, on {target}")
        self.poller.note_skill_ended(self.clock.now, "Trick_ActionSkill_Cannons_C")
        self.clock.advance(SKILL_WINDOW_S + 0.5)
        self.poller.note_grenade_thrown(self.clock.now)
        write_hit(self.controller, 2, 26.5, flags=0, target="Char_E_5")
        self.poller.tick()
        self.settle()
        self.assertEqual(self.received[-1]["src"], "grenade", "the skill is over; the throw window may have it again")

    def test_gun_grenade_and_skill_deal_damage_at_the_same_time(self) -> None:
        """A running action skill may not swallow the rest: gun hits carry the gun flag, an
        explosion names its own source, and a target the grenade is already burning keeps
        the grenade. Only what nothing else explains is the skill's."""
        self.poller.tick()
        self.poller.note_grenade_thrown(self.clock.now)
        self.clock.advance(0.5)
        write_hit(self.controller, 0, 14.6, flags=0, target="Char_Burning_1")
        self.poller.tick()
        self.assertEqual(self.received[-1]["src"], "grenade", "the area hits first")
        self.poller.note_skill_started(self.clock.now, "Trick_ActionSkill_Cannons_C")
        self.clock.advance(0.3)
        write_hit(self.controller, 1, 14.6, flags=0, target="Char_Burning_1")
        write_hit(self.controller, 2, 368.2, flags=WEAPON_FLAG, target="Char_Shot_2")
        write_hit(self.controller, 3, 26.5, flags=0, target="Char_Cannons_3")
        self.poller.tick()
        by_target = {event["target"]: (event["src"], event["why"]) for event in self.received[-3:]}
        self.assertEqual(by_target["Char_Burning_1"], ("grenade", "continued"), "the burning target stays the grenade's")
        self.assertEqual(by_target["Char_Shot_2"][0], "weapon", "the gun keeps its flagged hits")
        self.assertEqual(by_target["Char_Cannons_3"], ("skill", "skill-running"), "what nothing else explains is the skill's")
        self.poller.note_explosion(self.clock.now, GRENADE_SOURCE)
        write_hit(self.controller, 4, 146.4, flags=0, target="Char_Blasted_4")
        self.poller.tick()
        self.assertEqual(self.received[-1]["src"], "grenade", "an explosion names its own source, skill or not")

    def test_a_skill_trick_that_never_ends_is_dropped(self) -> None:
        self.poller.tick()
        self.poller.note_skill_started(self.clock.now, "Trick_ActionSkill_Stuck_C")
        self.clock.advance(SKILL_MAX_RUN_S + 1.0)
        write_hit(self.controller, 0, 26.5, flags=0, target="Char_A_1")
        self.poller.tick()
        self.settle()
        self.assertEqual(self.received[-1]["src"], "other", "a missed end may not own the rest of the session")

    def test_every_hit_says_which_rule_claimed_it(self) -> None:
        """The rule is written to the event, so a hit that looks wrong is explained by the
        log instead of by a probe run."""
        self.poller.tick()
        # The bullet and its elemental half land in the same frame.
        write_hit(self.controller, 0, 50.0, flags=WEAPON_FLAG, target="Char_A_1")
        write_hit(self.controller, 1, 9.0, flags=0, target="Char_A_1", dtype="Fire")
        self.poller.tick()
        self.assertEqual([event["why"] for event in self.received[-2:]], ["gun-flag", "same-frame"])
        self.clock.advance(1.0)
        write_hit(self.controller, 2, 4.0, flags=0, target="Char_A_1", dtype="Fire_Status", is_dot=True)
        self.poller.tick()
        self.assertEqual(self.received[-1]["why"], "status")
        self.poller.note_grenade_thrown(self.clock.now)
        self.clock.advance(1.0)
        write_hit(self.controller, 3, 333.0, flags=0, target="Char_B_2")
        self.poller.tick()
        self.assertEqual(self.received[-1]["why"], "grenade-window")
        self.clock.advance(0.3)
        self.poller.note_skill_activity(self.clock.now)
        write_hit(self.controller, 4, 333.0, flags=0, target="Char_B_2")
        self.poller.tick()
        self.assertEqual(self.received[-1]["why"], "continued")
        self.clock.advance(20.0)
        write_hit(self.controller, 5, 333.0, flags=0, target="Char_C_3")
        self.poller.tick()
        self.settle()
        self.assertEqual(self.received[-1]["why"], "none")

    def test_a_burning_target_keeps_the_grenade_when_a_skill_starts(self) -> None:
        """A corrosive cloud ticking three times a second was taken over by the skill window
        in the middle of its stream (session of 2026-09-22): the target it is burning keeps
        it, a target it never hit does not."""
        self.poller.tick()
        self.poller.note_grenade_thrown(self.clock.now)
        self.clock.advance(1.0)
        write_hit(self.controller, 0, 12.8, flags=0, target="Char_CatElder_1")
        self.poller.tick()
        self.assertEqual(self.received[-1]["src"], "grenade")
        self.poller.note_skill_activity(self.clock.now)
        self.clock.advance(0.3)
        write_hit(self.controller, 1, 12.7, flags=0, target="Char_CatElder_1")
        write_hit(self.controller, 2, 240.0, flags=0, target="Char_CatElder_2")
        self.poller.tick()
        by_target = {event["target"]: event["src"] for event in self.received[-2:]}
        self.assertEqual(by_target["Char_CatElder_1"], "grenade", "the burning target keeps its source")
        self.assertEqual(by_target["Char_CatElder_2"], "skill", "a target the grenade never hit is the skill's")

    def test_the_grenade_stream_ends_when_its_hits_stop(self) -> None:
        self.poller.tick()
        self.poller.note_grenade_thrown(self.clock.now)
        write_hit(self.controller, 0, 12.8, flags=0, target="Char_CatElder_1")
        self.poller.tick()
        self.poller.note_skill_activity(self.clock.now)
        self.clock.advance(STREAM_CONTINUATION_S + 0.5)
        self.poller.note_skill_activity(self.clock.now)
        write_hit(self.controller, 1, 240.0, flags=0, target="Char_CatElder_1")
        self.poller.tick()
        self.assertEqual(self.received[-1]["src"], "skill", "the area stopped hitting it; the skill may have it")

    def test_a_skill_token_alone_does_not_open_the_skill_window(self) -> None:
        """AddToTokenStack is a static call and says nothing about whose skill it was."""
        self.poller.tick()
        self.poller.note_grenade_thrown(self.clock.now)
        self.poller.note_skill_token(self.clock.now)
        self.clock.advance(0.5)
        write_hit(self.controller, 0, 12.8, flags=0, target="Char_A_1")
        self.poller.tick()
        self.assertEqual(self.received[-1]["src"], "grenade", "a token from anywhere is not the player's skill")
        self.poller.note_skill_activity(self.clock.now)
        self.clock.advance(2.5)
        self.poller.note_skill_token(self.clock.now)
        self.clock.advance(2.0)
        write_hit(self.controller, 1, 240.0, flags=0, target="Char_B_2")
        self.poller.tick()
        self.assertEqual(self.received[-1]["src"], "skill", "a token keeps a running skill open")

    def test_the_skill_window_cannot_live_on_its_own_hits(self) -> None:
        self.poller.tick()
        self.poller.note_skill_activity(self.clock.now)
        signal_at = self.clock.now
        for step in range(80):
            self.clock.advance(0.5)
            write_hit(self.controller, step % 6, 12.0, flags=0, target=f"Char_Cat_{step}")
            self.poller.tick()
        self.settle()
        late = [event for event in self.received if event["t"] > signal_at + SKILL_GRACE_CAP_S + SKILL_HIT_GRACE_S]
        self.assertTrue(late, "the fight ran past the cap")
        self.assertEqual({event["src"] for event in late}, {"other"}, "the grace cannot outlive the skill signal")

    def test_the_swing_end_closes_the_melee_window_early(self) -> None:
        self.poller.tick()
        self.poller.note_skill_activity(self.clock.now)
        self.poller.note_melee_swing(self.clock.now)
        self.clock.advance(0.3)
        write_hit(self.controller, 0, 7552.1, flags=0, target="Char_A_1")
        self.poller.tick()
        self.clock.advance(0.3)
        self.poller.note_melee_end(self.clock.now)
        self.clock.advance(0.2)
        write_hit(self.controller, 1, 10136.0, flags=0, target="Char_B_2")
        self.poller.tick()
        self.assertEqual([e["src"] for e in self.received], ["melee", "skill"])

    def test_a_knife_swing_during_a_skill_is_melee_for_one_second(self) -> None:
        self.poller.tick()
        self.poller.note_skill_activity(self.clock.now)
        self.clock.advance(0.5)
        self.poller.note_melee_swing(self.clock.now)
        self.clock.advance(0.4)
        write_hit(self.controller, 0, 7552.1, flags=0, target="Char_A_1")
        write_hit(self.controller, 1, 7552.1, flags=0, target="Char_B_2")
        self.poller.tick()
        self.assertEqual([e["src"] for e in self.received], ["melee", "melee"])
        self.clock.advance(0.8)
        self.poller.note_skill_activity(self.clock.now)
        write_hit(self.controller, 2, 10136.0, flags=0, target="Char_A_1")
        self.poller.tick()
        self.assertEqual(self.received[-1]["src"], "skill")

    def test_a_sticky_that_detonates_while_no_gun_is_in_hand_goes_to_the_gun_that_stuck_it(self) -> None:
        torgue = fake_weapon(281, "/Game/Gear/Weapons/Shotguns/TOR/Body_TOR_SG.Body_TOR_SG")
        self.hold(torgue)
        self.poller.tick()
        write_hit(self.controller, 0, 41.2, flags=WEAPON_FLAG, target="Char_StrikerBasic_1")
        self.poller.tick()
        self.clock.advance(1.0)
        self.hold(None)  # mid swap: the active slot is empty for a moment
        self.clock.advance(2.0)
        write_hit(self.controller, 1, 1338.5, flags=WEAPON_FLAG | KILL_FLAG, target="Char_StrikerBasic_1")
        self.poller.tick()
        self.assertEqual([e["weapon"] for e in self.received], ["Torgue Shotgun", "Torgue Shotgun"])
        # A flagged hit on a target the gun never touched falls back to the gun swapped away.
        write_hit(self.controller, 2, 300.0, flags=WEAPON_FLAG, target="Char_Other_2")
        self.poller.tick()
        self.assertEqual(self.received[-1]["weapon"], "Torgue Shotgun")

    # ------------------------------------------------------------------ the swap rule

    def test_sticky_detonation_after_a_swap_goes_to_the_previous_gun(self) -> None:
        torgue = fake_weapon(281, TORGUE_PATH)
        sniper = fake_weapon(277, SNIPER_PATH)
        self.hold(torgue)
        self.poller.tick()
        write_hit(self.controller, 0, 24.1, flags=WEAPON_FLAG | 0x10, target="Char_SoldierShotgun")
        self.poller.tick()
        self.clock.advance(0.5)
        self.hold(sniper)
        self.clock.advance(0.3)
        write_hit(self.controller, 1, 240.8, flags=WEAPON_FLAG | 0x10, target="Char_SoldierShotgun")
        self.poller.tick()
        self.assertEqual([e["weapon"] for e in self.received], ["Torgue Shotgun", "Torgue Shotgun"])
        # The sniper's own hit on the same target, once the grace is over, is the sniper's.
        self.clock.advance(SWAP_GRACE_S)
        write_hit(self.controller, 2, 394.0, flags=WEAPON_FLAG, target="Char_SoldierShotgun")
        self.poller.tick()
        self.assertEqual(self.received[-1]["weapon"], "Jakobs Sniper")

    def test_swap_rule_needs_a_recent_hit_by_the_previous_gun_and_the_grace(self) -> None:
        torgue = fake_weapon(281, TORGUE_PATH)
        sniper = fake_weapon(277, SNIPER_PATH)
        self.hold(torgue)
        self.poller.tick()
        write_hit(self.controller, 0, 24.1, flags=WEAPON_FLAG, target="Char_A_1")
        self.poller.tick()
        self.hold(sniper)
        self.clock.advance(SWAP_GRACE_S + 0.1)
        write_hit(self.controller, 1, 240.8, flags=WEAPON_FLAG, target="Char_A_1")
        write_hit(self.controller, 2, 240.8, flags=WEAPON_FLAG, target="Char_Fresh_2")
        self.poller.tick()
        self.assertEqual([e["weapon"] for e in self.received[1:]], ["Jakobs Sniper", "Jakobs Sniper"])


if __name__ == "__main__":
    unittest.main()
