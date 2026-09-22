"""Replay a recorded fight through the poller, before and after the attribution fix.

The hits come from a session log (time, damage, flags, damage type, kind, target) and the
signals from a probe run that recorded them by the second (grenade throws and explosions,
skill tokens, action skill signals: `poc/runs/*_attribution_evidence/probe_output.txt` or
any probe output with a "timeline by the second" section). Both are fed to a real
`DamagePoller` against a fake controller, once with the rules as they were and once with
the rules as they are, and the streams that change owner are counted for each.

    python poc/replay_attribution.py <session.jsonl> <probe_output.txt> [HH:MM:SS HH:MM:SS]

The last two arguments narrow the replay to one fight. Research tooling; reads only.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dmgtracker import poller as poller_module  # noqa: E402
from dmgtracker.attribution import WeaponResolver  # noqa: E402
from dmgtracker.poller import DamagePoller  # noqa: E402
from check_attribution import flips  # noqa: E402

RING_PROPERTY = "RecentlyCausedDamageInstances"
RING_SIZE = 6
TIMELINE_LINE = re.compile(r"^\s+(\d\d:\d\d:\d\d)\s+(.+)$")
SIGNAL_COUNT = re.compile(r"([a-z-]+)x(\d+)")
STATUS_SUFFIX = "_Status"


class Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def fake_instance(hit: dict) -> SimpleNamespace:
    is_dot = hit.get("kind") == "dot" or str(hit.get("dtype", "")).endswith(STATUS_SUFFIX)
    return SimpleNamespace(
        RepDirtyFlag=0,
        TotalDamage=float(hit["dmg"]),
        DamageFlags=SimpleNamespace(Flags=int(hit.get("flags", 0))),
        DamageType=SimpleNamespace(bIsDoT=is_dot, bIsHealingType=False, ElementalType=int(hit.get("elem", 0)), _name=str(hit.get("dtype", "Normal"))),
        DamagedActor=SimpleNamespace(Name=str(hit.get("target", ""))),
    )


def fake_controller() -> SimpleNamespace:
    weapon = SimpleNamespace(
        Item=SimpleNamespace(data=SimpleNamespace(InstanceId=1)),
        BodyData=SimpleNamespace(_path_name=lambda: "/Game/Body_BOR_SM.Body_BOR_SM"),
        Class=SimpleNamespace(Name="OakWeapon"),
    )
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
    setattr(controller, RING_PROPERTY, [fake_instance({"dmg": 0.0, "target": ""}) for _ in range(RING_SIZE)])
    return controller


def read_hits(path: Path) -> list[dict]:
    hits = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith('{"t"') and '"dmg"' in line:
            try:
                hits.append(json.loads(line))
            except ValueError:
                continue
    return sorted(hits, key=lambda hit: float(hit["t"]))


def read_signals(path: Path, day: float) -> list[tuple[float, str]]:
    """(epoch second, signal name) from a probe's timeline section, on the log's day."""
    signals: list[tuple[float, str]] = []
    in_timeline = False
    midnight = time.mktime(time.localtime(day)[:3] + (0, 0, 0, 0, 0, -1))
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "timeline by the second" in line:
            in_timeline = True
            continue
        if not in_timeline:
            continue
        match = TIMELINE_LINE.match(line)
        if match is None:
            continue
        hours, minutes, seconds = (int(part) for part in match.group(1).split(":"))
        when = midnight + hours * 3600 + minutes * 60 + seconds
        for name, count in SIGNAL_COUNT.findall(match.group(2)):
            signals.extend([(when, name)] * int(count))
    return sorted(signals)


def deliver(poller: DamagePoller, name: str, now: float, tokens_open_the_window: bool) -> None:
    if name == "grenade-throw":
        poller.note_grenade_thrown(now)
    elif name in ("grenade-explode", "projectile-explode"):
        poller.note_grenade_exploded(now)
    elif name == "skill-action":
        poller.note_skill_activity(now)
    elif name == "skill-token":
        if tokens_open_the_window:
            poller.note_skill_activity(now)
        else:
            poller.note_skill_token(now)


def replay(hits: list[dict], signals: list[tuple[float, str]], tokens_open_the_window: bool, continuation_s: float) -> list[dict]:
    original_continuation = poller_module.STREAM_CONTINUATION_S
    poller_module.STREAM_CONTINUATION_S = continuation_s
    try:
        clock = Clock(float(hits[0]["t"]) - 1.0)
        controller = fake_controller()
        received: list[dict] = []
        poller = DamagePoller(
            resolve_controller=lambda: controller,
            ring_property=RING_PROPERTY,
            ring_size=RING_SIZE,
            resolver=WeaponResolver(),
            sink=received.extend,
            clock=clock,
        )
        poller.tick()
        pending = list(signals)
        for index, hit in enumerate(hits):
            now = float(hit["t"])
            while pending and pending[0][0] <= now:
                when, name = pending.pop(0)
                clock.now = when
                deliver(poller, name, when, tokens_open_the_window)
            clock.now = now
            slot = index % RING_SIZE
            ring = getattr(controller, RING_PROPERTY)
            previous = ring[slot].RepDirtyFlag
            ring[slot] = fake_instance(hit)
            ring[slot].RepDirtyFlag = (previous + 1) % 256
            poller.tick()
        clock.now += poller_module.PENDING_HOLD_S + 0.05
        poller.tick()
        return received
    finally:
        poller_module.STREAM_CONTINUATION_S = original_continuation


def summarise(title: str, events: list[dict]) -> int:
    by_source: dict[str, float] = {}
    for event in events:
        by_source[str(event.get("weapon"))] = by_source.get(str(event.get("weapon")), 0.0) + float(event["dmg"])
    found = flips(events)
    print(f"{title}: {len(events)} hits, {len(found)} stream(s) that changed owner")
    for name, damage in sorted(by_source.items(), key=lambda pair: -pair[1]):
        print(f"      {name:<12} {damage:10.1f}")
    for flip in found:
        when = time.strftime("%H:%M:%S", time.localtime(flip["t"]))
        print(f"      {when} {flip['target'][:32]:<32} {flip['dtype']:<18} {flip['from']} ({flip['from_why']}) -> {flip['to']} ({flip['to_why']})")
    return len(found)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    hits = read_hits(Path(argv[0]))
    if not hits:
        print("no hits in that log")
        return 1
    signals = read_signals(Path(argv[1]), float(hits[0]["t"]))
    if len(argv) >= 4:
        midnight = time.mktime(time.localtime(float(hits[0]["t"]))[:3] + (0, 0, 0, 0, 0, -1))

        def at(text: str) -> float:
            hours, minutes, seconds = (int(part) for part in text.split(":"))
            return midnight + hours * 3600 + minutes * 60 + seconds

        start, end = at(argv[2]), at(argv[3])
        hits = [hit for hit in hits if start <= float(hit["t"]) <= end]
        signals = [signal for signal in signals if start - 15 <= signal[0] <= end]
    print(f"replaying {len(hits)} hit(s) with {len(signals)} signal(s)")
    before = summarise("   as the rules were (a token opens the skill window, no continuation)",
                       replay(hits, signals, tokens_open_the_window=True, continuation_s=0.0))
    after = summarise("   as the rules are  (a token only keeps one open, a burning target keeps its source)",
                      replay(hits, signals, tokens_open_the_window=False, continuation_s=poller_module.STREAM_CONTINUATION_S))
    print(f"   flips before {before}, after {after}")
    return 0 if after <= before else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
