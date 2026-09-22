"""Reload the mod from the repo without restarting the game.

Drop this into `poc/inbox/` (the probe runner executes it on the game thread) whenever mod
code changed: it disables the running mod, which removes its hooks, stops the dashboard,
saves the weapon record and the item names and closes the session log, drops every
`dmgtracker` module from `sys.modules`, imports the package again (the SDK's `build_mod`
deregisters the old entry from the mod menu) and enables the new one.

What survives: the stores on disk and the game itself. What starts over: the session log
(a new file), the live table's window, the open fight, the item name reader's remembered
addresses, and the dashboard connection (reload the page).

A scan thread of the old code may still be running for a moment; it writes into the old
reader and is harmless. Research tooling; not part of the mod.
"""
import importlib, sys, time
from pathlib import Path
from unrealsdk import logging
import mods_base

OUT = Path(__file__).parent / "probe_output.txt"
PACKAGE = "dmgtracker"


def say(m):
    logging.info("[reload] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def version_of(module):
    try:
        return module.mod.version
    except Exception:
        return "?"


say(f"{time.strftime('%H:%M:%S')} reloading {PACKAGE} from {Path(sys.modules[PACKAGE].__file__).parent}")
previous = sys.modules.get(f"{PACKAGE}.mod")
old_mod = getattr(previous, "mod", None)
if old_mod is not None:
    say(f"   the running mod is {version_of(previous)}, enabled {old_mod.is_enabled}")
    try:
        old_mod.disable()
    except Exception as ex:
        say(f"   disable failed: {type(ex).__name__}: {ex}")
    try:
        mods_base.deregister_mod(old_mod)
    except Exception as ex:
        say(f"   deregister skipped: {type(ex).__name__}: {ex}")

purged = [name for name in list(sys.modules) if name == PACKAGE or name.startswith(PACKAGE + ".")]
for name in purged:
    del sys.modules[name]
say(f"   {len(purged)} module(s) dropped")

try:
    package = importlib.import_module(PACKAGE)
except Exception as ex:
    say(f"   import failed: {type(ex).__name__}: {ex}")
    import traceback
    for line in traceback.format_exc().splitlines():
        say("      " + line)
    say("done")
else:
    fresh = sys.modules[f"{PACKAGE}.mod"]
    new_mod = fresh.mod
    # mods_base.mod_list is the submodule; the list itself lives inside it.
    registered = new_mod in getattr(mods_base.mod_list, "mod_list", [])
    say(f"   imported {version_of(fresh)}; registered {registered}")
    try:
        new_mod.enable()
    except Exception as ex:
        say(f"   enable failed: {type(ex).__name__}: {ex}")
        import traceback
        for line in traceback.format_exc().splitlines():
            say("      " + line)
    else:
        poller = getattr(fresh, "poller", None)
        dashboard = getattr(fresh, "dashboard", None)
        rules = getattr(sys.modules[f"{PACKAGE}.poller"], "RULE_BURNING", None)
        say(f"   enabled {new_mod.is_enabled}; poller {poller is not None}, dashboard {dashboard is not None}"
            f", log {getattr(getattr(fresh, 'session_log', None), 'path', None)}")
        say(f"   the new attribution rules are live: {rules!r}")
    say("done")
