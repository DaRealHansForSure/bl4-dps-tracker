"""Record everything attribution is decided from, so a fight can be replayed forever.

The session log holds the mod's conclusions; this holds its inputs. Every damage instance
as it was read (damage, flags, damage type, status or not, element, target, frame) and
every signal the mod received (grenade throws and explosions, projectile explosions with
their owner, melee and skill signals, tokens, the weapon in hand) go to

    poc/captures/capture-<time>.jsonl

together with what the running mod decided for each hit, so a replay can compare. Nothing
is written on the game thread: the lines are appended to a list and a background thread
writes them every two seconds.

Drop into `poc/inbox/` and play. Runs ten minutes unless MINUTES below is changed. Replay
it afterwards with `python poc/replay_capture.py <file>`, as often as the rules change.
"""
import json, sys, time, threading
from pathlib import Path
from unrealsdk import logging

MINUTES = 15.0
FLUSH_INTERVAL_S = 2.0
OUT = Path(__file__).parent / "probe_output.txt"
# The probe runner copies this file into poc/runs/<run>/, so __file__ is two levels below
# poc/; walk up until the folder that holds this script's own directory is found.
CAPTURES = (Path(__file__).resolve().parents[2] if Path(__file__).resolve().parent.parent.name == "runs"
            else Path(__file__).resolve().parent) / "captures"
STATUS_SUFFIX = "_Status"

_lines = []
_lock = threading.Lock()


def say(m):
    logging.info("[record] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def add(line):
    with _lock:
        _lines.append(line)


def source_json(source):
    try:
        return {"kind": source.kind, "label": source.label, "body": source.body, "wid": source.item_id}
    except Exception:
        return {"kind": "?"}


mod = sys.modules["dmgtracker.mod"]
poller = mod.poller
if poller is None:
    say("no poller: the mod must be enabled and you must be in the world")
    say("done")
    raise SystemExit

CAPTURES.mkdir(parents=True, exist_ok=True)
path = CAPTURES / f"capture-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
add({"capture": {"started": time.time(), "version": getattr(mod, "_mod_version", lambda: "?")(), "ring": poller._ring_size}})

original_read = poller._read_instance


def read_instance(instance, pawn, now, flagged_targets):
    event = original_read(instance, pawn, now, flagged_targets)
    try:
        damage_type = instance.DamageType
        type_name = str(getattr(damage_type, "_name", "None"))
        add({"t": now, "frame": poller.ticks, "hit": {
            "dmg": float(instance.TotalDamage),
            "flags": int(instance.DamageFlags.Flags),
            "dtype": type_name,
            "dot": bool(getattr(damage_type, "bIsDoT", False)) or type_name.endswith(STATUS_SUFFIX),
            "heal": bool(getattr(damage_type, "bIsHealingType", False)),
            "elem": int(getattr(damage_type, "ElementalType", 0) or 0),
            "target": str(getattr(instance.DamagedActor, "Name", "")) if instance.DamagedActor is not None else "",
        }, "live": {"src": event.get("src"), "why": event.get("why"), "weapon": event.get("weapon"), "wid": event.get("wid")}})
    except Exception as ex:  # the recorder may never break the hot path
        add({"t": now, "error": f"{type(ex).__name__}: {ex}"})
    return event


poller._read_instance = read_instance
_installed = read_instance

SIGNALS = ("note_grenade_thrown", "note_grenade_exploded", "note_explosion", "note_melee_swing",
           "note_melee_end", "note_skill_activity", "note_skill_token", "note_skill_started",
           "note_skill_ended", "note_weapon_in_hand")
originals = {}
for name in SIGNALS:
    if not hasattr(poller, name):
        continue
    originals[name] = getattr(poller, name)

    def make(name, original):
        def wrapper(now, *rest):
            try:
                line = {"t": now, "sig": name}
                if rest:
                    line["with"] = source_json(rest[0])
                add(line)
            except Exception:
                pass
            return original(now, *rest)
        return wrapper

    setattr(poller, name, make(name, originals[name]))

say(f"{time.strftime('%H:%M:%S')} recording to {path.name} for {MINUTES:.0f} minutes; play a fight with a grenade and the action skill")


def writer():
    finish_at = time.time() + MINUTES * 60.0
    with path.open("w", encoding="utf-8") as handle:
        while True:
            time.sleep(FLUSH_INTERVAL_S)
            with _lock:
                batch, _lines[:] = list(_lines), []
            for line in batch:
                handle.write(json.dumps(line) + "\n")
            handle.flush()
            if poller._read_instance is not _installed:
                # Another probe restored the hot path and dropped this recorder with it.
                say("   the hot path was taken over by something else; recording stops here")
                break
            if time.time() >= finish_at:
                break
        poller._read_instance = original_read
        for name, original in originals.items():
            setattr(poller, name, original)
        with _lock:
            batch, _lines[:] = list(_lines), []
        for line in batch:
            handle.write(json.dumps(line) + "\n")
    hits = sum(1 for _ in path.read_text(encoding="utf-8").splitlines() if '"hit"' in _)
    say(f"   recorded {hits} hit(s) to {path}")
    say("done")


threading.Thread(target=writer, name="record-attribution", daemon=True).start()
