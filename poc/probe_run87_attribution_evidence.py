"""Everything needed to attribute damage correctly, in one run: the signals with their
callers, and every field of the damage instances they explain.

Part A hooks the seven attribution signals (trick begin and end, skill action, skill token,
grenade throw, grenade explode, projectile explode) and records the caller's class and, when
the object offers one, its owner: an enemy's ActionSkill trick or a foreign token opening
the player's skill window would show here.

Part B wraps the mod's own `_read_instance` and dumps every field of the ring struct for the
first hits, with the damage type's full asset path: a field that tells a grenade's area
from a skill's damage would end the window guessing for good.

Runs for ten minutes, prints a timeline by the second and a summary. Just play: throw a
grenade into a fight and let it burn, use the action skill, shoot, melee.
"""
import sys, time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probe87"
WINDOW_S = 600.0
MAX_EVENTS = 8000
MAX_DUMPS = 24
MAX_VALUE_CHARS = 90

SIGNALS = (
    ("trick-begin", "/Script/GbxGame.GbxTrickScript:OnBegin_Mut"),
    ("trick-end", "/Script/GbxGame.GbxTrickScript:OnEnd_Mut"),
    ("skill-action", "/Script/GbxGame.GbxSkillScript:K2_StartSkillAction"),
    ("skill-token", "/Script/OakGame.GbxSkillTokenProviderStatics:AddToTokenStack"),
    ("grenade-throw", "/Script/OakGame.OakCharacter:ThrowGadget"),
    ("grenade-explode", "/Script/OakGame.GrenadeGadget:ExplodeCallback"),
    ("projectile-explode", "/Script/GbxGame.projectile:Explode"),
)

_events = []
_dumps = 0
_lock = threading.Lock()


def say(m):
    logging.info("[evidence87] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def owner_text(obj):
    """The object's owner, when it has a getter: this is what says whose skill it was."""
    try:
        owner = obj.GetOwner()
    except Exception:
        return "no owner getter"
    if owner is None:
        return "owner None"
    try:
        return f"owner {owner.Class.Name} {owner.Name}"
    except Exception:
        return "owner unreadable"


def make_callback(name):
    def callback(obj, args, ret, func):
        try:
            try:
                what = f"{obj.Class.Name}, {owner_text(obj)}"
            except Exception:
                what = "?"
            with _lock:
                if len(_events) < MAX_EVENTS:
                    _events.append((time.time(), name, what))
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


def value_text(value):
    try:
        return repr(value)[:MAX_VALUE_CHARS]
    except Exception as ex:
        return f"<{type(ex).__name__}>"


def field_names(struct):
    names = []
    try:
        for field in struct._type._fields():
            try:
                if str(field.Class.Name) != "Function":
                    names.append(str(field.Name))
            except Exception:
                continue
    except Exception as ex:
        say(f"   cannot list the struct's fields: {type(ex).__name__}: {ex}")
    return names


def dump(instance, event):
    say(f"   {time.strftime('%H:%M:%S')} hit {event['dmg']:.1f} {event['kind']} src={event['src']} dtype={event['dtype']} target={event['target']} flags={event['flags']}")
    for name in field_names(instance):
        try:
            value = getattr(instance, name)
        except Exception as ex:
            say(f"      {name:<28} <{type(ex).__name__}: {ex}>"[:200])
            continue
        if hasattr(value, "_path_name"):
            try:
                say(f"      {name:<28} {value.Class.Name} {value._path_name()}"[:220])
            except Exception:
                say(f"      {name:<28} {value_text(value)}")
        else:
            say(f"      {name:<28} {value_text(value)}")
    try:
        damage_type = instance.DamageType
        say(f"      DamageType                  {damage_type.Class.Name} {damage_type._path_name()}"[:220])
        for name in ("ElementalType", "bIsDoT", "bIsHealingType", "StatusEffectType", "DamageSourceType", "DamageSource"):
            try:
                say(f"         {name:<24} {value_text(getattr(damage_type, name))}")
            except Exception as ex:
                say(f"         {name:<24} <{type(ex).__name__}>")
    except Exception as ex:
        say(f"      DamageType unreadable: {type(ex).__name__}: {ex}")


mod = sys.modules["dmgtracker.mod"]
poller = mod.poller
original = poller._read_instance


def wrapped(instance, pawn, now, flagged_targets):
    event = original(instance, pawn, now, flagged_targets)
    global _dumps
    try:
        with _lock:
            room = _dumps < MAX_DUMPS
            if room:
                _dumps += 1
        if room:
            dump(instance, event)
    except Exception as ex:  # the probe may never break the mod's hot path
        say(f"   dump failed: {type(ex).__name__}: {ex}")
    return event


poller._read_instance = wrapped
say(f"{time.strftime('%H:%M:%S')} attribution evidence: {len(installed)} signal hook(s) and the instance fields, for {WINDOW_S / 60:.0f} minutes")
say("   play normally: a grenade into a fight and let it burn, the action skill, shooting, melee")


def finish():
    time.sleep(WINDOW_S)
    poller._read_instance = original
    for path, identifier in installed:
        try:
            if unrealsdk.hooks.has_hook(path, Type.PRE, identifier):
                unrealsdk.hooks.remove_hook(path, Type.PRE, identifier)
        except Exception as ex:
            say(f"   cannot remove the hook on {path}: {type(ex).__name__}: {ex}")
    with _lock:
        events = list(_events)
    say(f"   over: {len(events)} signal(s), {_dumps} hit dump(s)")
    for name, count in collections.Counter(name for _when, name, _what in events).most_common():
        say(f"      {count:>6} x {name}")
    say("   by caller:")
    for (name, what), count in collections.Counter((name, what) for _when, name, what in events).most_common(25):
        say(f"      {count:>6} x {name} <- {what}")
    say("   timeline by the second:")
    per_second = collections.defaultdict(collections.Counter)
    for when, name, _what in events:
        per_second[int(when)][name] += 1
    for second in sorted(per_second):
        text = " ".join(f"{name}x{count}" for name, count in sorted(per_second[second].items()))
        say(f"      {time.strftime('%H:%M:%S', time.localtime(second))} {text}")
    say("done")


threading.Thread(target=finish, name="evidence87", daemon=True).start()
