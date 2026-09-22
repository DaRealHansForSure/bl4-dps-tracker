"""Log every function the game calls, in a window you control, so the grenade's pipeline
and the skill's can be told apart.

The engine's damage events do not fire in this game (probe a4) and the damage ring names no
causer, so the mod infers. This is the way out: the SDK can trace every UFunction call.
Run it twice, doing exactly one thing each time, and the functions that appear in one trace
and not in the other are the signal the mod should hook.

    pass 1: throw a grenade, nothing else
    pass 2: use the action skill, nothing else

The trace goes to Plugins/unrealsdk.calls.tsv and is written for TRACE_SECONDS only,
because it costs a lot of frames. Markers with the exact times go to this run's output so
the trace can be cut to the window afterwards.
"""
import time, threading
from pathlib import Path
import unrealsdk
from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"
WAIT_BEFORE_S = 6.0
TRACE_SECONDS = 10.0


def say(m):
    logging.info("[trace] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def set_logging(enabled):
    """Turn the SDK's call trace on or off, whichever way this build offers."""
    for owner, name in ((unrealsdk.hooks, "log_all_calls"), (unrealsdk, "log_all_calls")):
        function = getattr(owner, name, None)
        if callable(function):
            function(enabled)
            return f"{owner.__name__}.{name}"
    raise AttributeError("this build has no log_all_calls")


say(f"{time.strftime('%H:%M:%S')} call trace: {WAIT_BEFORE_S:.0f} s to get ready, then {TRACE_SECONDS:.0f} s of tracing")
say("   do exactly ONE thing in the window: this pass is for a single grenade throw, the next for the action skill")


def run():
    time.sleep(WAIT_BEFORE_S)
    try:
        how = set_logging(True)
    except Exception as ex:
        say(f"   cannot start the trace: {type(ex).__name__}: {ex}")
        say("done")
        return
    started = time.time()
    say(f"   TRACE ON at {time.strftime('%H:%M:%S', time.localtime(started))}.{int(started % 1 * 1000):03d} (via {how}) - go")
    time.sleep(TRACE_SECONDS)
    try:
        set_logging(False)
    except Exception as ex:
        say(f"   cannot stop the trace: {type(ex).__name__}: {ex}")
    stopped = time.time()
    say(f"   TRACE OFF at {time.strftime('%H:%M:%S', time.localtime(stopped))}.{int(stopped % 1 * 1000):03d}")
    say(f"   window: {started:.3f} to {stopped:.3f}")
    say("done")


threading.Thread(target=run, name="trace-c3", daemon=True).start()
