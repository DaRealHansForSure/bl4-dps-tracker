"""Replay a capture through the current attribution rules, as often as they change.

A capture (`poc/record_attribution.py`) holds the inputs the mod decided from: every damage
instance as it was read, frame by frame, and every signal it received. This feeds them to a
real `DamagePoller` against a fake controller and prints what the rules make of them now,
next to what the mod decided while recording, plus the streams that change owner.

    python poc/replay_capture.py                      the newest capture
    python poc/replay_capture.py <capture.jsonl>      one capture
    python poc/replay_capture.py <capture.jsonl> -v   with one line per hit that differs

Exit code 1 when a stream changes owner, so it can guard a change to the rules.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dmgtracker import poller as poller_module  # noqa: E402
from dmgtracker.attribution import DamageSource, WeaponResolver  # noqa: E402
from dmgtracker.poller import DamagePoller  # noqa: E402
from check_attribution import flips  # noqa: E402

CAPTURES = Path(__file__).resolve().parent / "captures"
RING_PROPERTY = "RecentlyCausedDamageInstances"
DEFAULT_RING_SIZE = 6


class Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def instance_of(hit: dict) -> SimpleNamespace:
    return SimpleNamespace(
        RepDirtyFlag=0,
        TotalDamage=float(hit["dmg"]),
        DamageFlags=SimpleNamespace(Flags=int(hit.get("flags", 0))),
        DamageType=SimpleNamespace(
            bIsDoT=bool(hit.get("dot")),
            bIsHealingType=bool(hit.get("heal")),
            ElementalType=int(hit.get("elem", 0)),
            _name=str(hit.get("dtype", "Normal")),
        ),
        DamagedActor=SimpleNamespace(Name=str(hit.get("target", ""))),
    )


def weapon_of(held: dict) -> SimpleNamespace:
    return SimpleNamespace(
        Item=SimpleNamespace(data=SimpleNamespace(InstanceId=abs(hash(held.get("wid", ""))) % 100000)),
        BodyData=SimpleNamespace(_path_name=lambda: held.get("body") or "/Game/Body_BOR_SM.Body_BOR_SM"),
        Class=SimpleNamespace(Name="OakWeapon"),
    )


def controller_of(weapon: SimpleNamespace, ring_size: int) -> SimpleNamespace:
    pawn = SimpleNamespace(
        Class=SimpleNamespace(Name="OakCharacter"),
        ActiveWeapons=SimpleNamespace(Slots=[SimpleNamespace(Weapon=weapon)]),
        EquippedInventorySlots=SimpleNamespace(items=[]),
    )
    controller = SimpleNamespace(
        Pawn=pawn,
        Class=SimpleNamespace(Name="OakPlayerController"),
        _path_name=lambda: "/Game/Maps/World_P:PersistentLevel.OakPlayerController_1",
        _get_address=lambda: 0x1000,
    )
    setattr(controller, RING_PROPERTY, [instance_of({"dmg": 0.0, "target": ""}) for _ in range(ring_size)])
    return controller


def read_capture(path: Path) -> tuple[dict, list[dict]]:
    header: dict = {}
    lines: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if "capture" in line:
            header = line["capture"]
        elif "hit" in line or "sig" in line:
            lines.append(line)
    return header, lines


def replay(header: dict, lines: list[dict], verbose: bool) -> list[dict]:
    ring_size = int(header.get("ring", DEFAULT_RING_SIZE))
    weapon = weapon_of({})
    controller = controller_of(weapon, ring_size)
    received: list[dict] = []
    clock = Clock(float(lines[0]["t"]) - 1.0)
    poller = DamagePoller(
        resolve_controller=lambda: controller,
        ring_property=RING_PROPERTY,
        ring_size=ring_size,
        resolver=WeaponResolver(),
        sink=received.extend,
        clock=clock,
    )
    poller.tick()
    frame: list[dict] = []

    def flush_frame() -> None:
        if not frame:
            return
        ring = getattr(controller, RING_PROPERTY)
        for slot, line in enumerate(frame[:ring_size]):
            previous = ring[slot].RepDirtyFlag
            ring[slot] = instance_of(line["hit"])
            ring[slot].RepDirtyFlag = (previous + 1) % 256
        clock.now = float(frame[-1]["t"])
        poller.tick()
        frame.clear()

    for line in lines:
        if "hit" in line:
            if frame and line.get("frame") != frame[-1].get("frame"):
                flush_frame()
            frame.append(line)
            continue
        flush_frame()
        clock.now = float(line["t"])
        name, payload = line["sig"], line.get("with")
        if name == "note_weapon_in_hand":
            held = payload or {}
            if held.get("kind") == "weapon":
                controller.Pawn.ActiveWeapons.Slots[0].Weapon = weapon_of(held)
            poller.note_weapon_in_hand(clock.now, DamageSource(held.get("kind", "unknown"), held.get("label", "Unknown"), held.get("body", ""), held.get("wid", "")))
        elif name == "note_explosion":
            source = payload or {}
            poller.note_explosion(clock.now, DamageSource(source.get("kind", "grenade"), source.get("label", "Grenade"), source.get("body", ""), source.get("wid", "")))
        else:
            method = getattr(poller, name, None)
            if method is not None:
                method(clock.now)
    flush_frame()
    clock.now += poller_module.PENDING_HOLD_S + 0.05
    poller.tick()
    return received


def compare(lines: list[dict], events: list[dict], verbose: bool) -> int:
    """How many hits the rules now attribute differently than the mod did while recording."""
    live = [line["live"] for line in lines if "hit" in line]
    changed = 0
    for index, event in enumerate(events):
        if index >= len(live):
            break
        was = live[index]
        if was.get("src") != event.get("src"):
            changed += 1
            if verbose:
                when = time.strftime("%H:%M:%S", time.localtime(float(event["t"])))
                print(f"      {when} {event['dmg']:>8.1f} {str(event.get('target'))[:30]:<30} "
                      f"{was.get('src')} ({was.get('why')}) -> {event.get('src')} ({event.get('why')})")
    return changed


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    paths = [Path(arg) for arg in argv if not arg.startswith("-")]
    if not paths:
        paths = sorted(CAPTURES.glob("capture-*.jsonl"))[-1:]
    if not paths:
        print(f"no captures in {CAPTURES}; record one with poc/record_attribution.py")
        return 1
    worst = 0
    for path in paths:
        header, lines = read_capture(path)
        if not lines:
            print(f"{path.name}: empty")
            continue
        events = replay(header, lines, verbose)
        hits = sum(1 for line in lines if "hit" in line)
        signals = sum(1 for line in lines if "sig" in line)
        print(f"{path.name}: {hits} hit(s), {signals} signal(s), recorded by {header.get('version', '?')}")
        by_source: dict[str, float] = {}
        for event in events:
            by_source[str(event.get("weapon"))] = by_source.get(str(event.get("weapon")), 0.0) + float(event["dmg"])
        for name, damage in sorted(by_source.items(), key=lambda pair: -pair[1]):
            print(f"      {name:<14} {damage:12.1f}")
        changed = compare(lines, events, verbose)
        found = flips(events)
        print(f"   {len(found)} stream(s) change owner, {changed} hit(s) attributed differently than when recorded")
        for flip in found:
            when = time.strftime("%H:%M:%S", time.localtime(flip["t"]))
            print(f"      {when} {flip['target'][:32]:<32} {flip['dtype']:<18} "
                  f"{flip['from']} ({flip['from_why']}) -> {flip['to']} ({flip['to_why']})")
        worst = max(worst, len(found))
    return 1 if worst else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
