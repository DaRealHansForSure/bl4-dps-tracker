"""Does this character's action skill fire a signal that names its owner?

Run 87 proved the opposite case: a fight with no action skill in it, where three ownerless
`AddToTokenStack` calls opened the skill window and took a grenade's corrosive area with
them. Since a token may no longer open the window, the action skill must announce itself
some other way, or its damage would fall through to Other.

This logs the four skill signals for five minutes, with each caller's class and owner and
whether the mod would treat it as a strong opener (an ActionSkill trick, or a skill script
the player controller owns).

Use the action skill a few times. No grenades needed.
"""
import time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probe93"
WINDOW_S = 300.0
MAX_EVENTS = 4000
SKILL_TRICK_FRAGMENT = "ActionSkill"

SIGNALS = (
    ("trick-begin", "/Script/GbxGame.GbxTrickScript:OnBegin_Mut"),
    ("trick-end", "/Script/GbxGame.GbxTrickScript:OnEnd_Mut"),
    ("skill-action", "/Script/GbxGame.GbxSkillScript:K2_StartSkillAction"),
    ("skill-token", "/Script/OakGame.GbxSkillTokenProviderStatics:AddToTokenStack"),
)

_events = []
_lock = threading.Lock()


def say(m):
    logging.info("[skill93] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def controller_address():
    try:
        return int(get_pc()._get_address())
    except Exception:
        return 0


def describe(name, obj):
    """The caller's class, its owner, and whether the mod would call it a strong opener."""
    try:
        class_name = str(obj.Class.Name)
    except Exception:
        class_name = "?"
    owner_text = "no owner getter"
    owned_by_player = False
    try:
        owner = obj.GetOwner()
        if owner is None:
            owner_text = "owner None"
        else:
            owner_text = f"owner {owner.Class.Name} {owner.Name}"
            owned_by_player = int(owner._get_address()) == controller_address()
    except Exception:
        pass
    if name == "skill-action":
        strong = owned_by_player
    elif name in ("trick-begin", "trick-end"):
        strong = SKILL_TRICK_FRAGMENT in class_name
    else:
        strong = False
    return f"{class_name}, {owner_text}, {'OPENS the window' if strong else 'does not open it'}"


def make_callback(name):
    def callback(obj, args, ret, func):
        try:
            detail = describe(name, obj)
            with _lock:
                if len(_events) < MAX_EVENTS:
                    _events.append((time.time(), name, detail))
        except Exception:
            pass
    return callback


installed = []
for name, path in SIGNALS:
    identifier = f"{HOOK_ID}:{name}"
    try:
        unrealsdk.hooks.add_hook(path, Type.PRE, identifier, make_callback(name))
        installed.append((path, identifier))
    except Exception as ex:
        say(f"   cannot hook {path}: {type(ex).__name__}: {ex}")

say(f"{time.strftime('%H:%M:%S')} action skill signals; {len(installed)} hook(s) for {WINDOW_S / 60:.0f} minutes")
say("   use the action skill a few times")


def finish():
    time.sleep(WINDOW_S)
    for path, identifier in installed:
        try:
            if unrealsdk.hooks.has_hook(path, Type.PRE, identifier):
                unrealsdk.hooks.remove_hook(path, Type.PRE, identifier)
        except Exception as ex:
            say(f"   cannot remove the hook on {path}: {type(ex).__name__}: {ex}")
    with _lock:
        events = list(_events)
    say(f"   over: {len(events)} signal(s)")
    for (name, detail), count in collections.Counter((name, detail) for _when, name, detail in events).most_common(30):
        say(f"      {count:>5} x {name} <- {detail}")
    openers = [(when, name, detail) for when, name, detail in events if "OPENS" in detail]
    say(f"   {len(openers)} signal(s) would open the skill window:")
    for when, name, detail in openers[:20]:
        say(f"      {time.strftime('%H:%M:%S', time.localtime(when))} {name} <- {detail}")
    say("   timeline by the second:")
    per_second = collections.defaultdict(collections.Counter)
    for when, name, _detail in events:
        per_second[int(when)][name] += 1
    for second in sorted(per_second):
        text = " ".join(f"{name}x{count}" for name, count in sorted(per_second[second].items()))
        say(f"      {time.strftime('%H:%M:%S', time.localtime(second))} {text}")
    say("done")


threading.Thread(target=finish, name="skill93", daemon=True).start()
