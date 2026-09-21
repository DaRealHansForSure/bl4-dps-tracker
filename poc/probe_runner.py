"""
Probe runner: executes probe scripts dropped into poc/inbox/ on the game thread.

Run once with `pyexec probe_runner.py`. From then on, every .py file that appears in
poc/inbox/ is executed (oldest name first) as if by pyexec, inside its own folder
poc/runs/<time>_<name>/ where __file__ points, so each probe's probe_output.txt is that
run's log alone. Errors go there as well and never reach the game.

    pyexec probe_runner.py         install (safe to run again: it replaces itself)
    pyexec probe_runner_stop.py    remove the hook

Research tooling only; not part of the mod.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

POC_DIR = Path(__file__).parent
INBOX = POC_DIR / "inbox"
RUNS = POC_DIR / "runs"
OUT = POC_DIR / "probe_output.txt"
TICK_FUNCTION = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
HOOK_ID = "dmgtracker-probe-runner"
POLL_INTERVAL_S = 0.5
SETTLE_S = 1.0

_last_poll = 0.0


def say(msg: str) -> None:
    logging.info(f"[probe-runner] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def run_probe(path: Path) -> None:
    """Execute one probe file inside its own run folder (poc/runs/<time>_<name>/), where
    __file__ points, so the probe's probe_output.txt is that run's log alone."""
    run_dir = RUNS / f"{time.strftime('%H%M%S')}_{path.stem}"
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        script = run_dir / path.name
        path.rename(script)
    except OSError as ex:
        say(f"could not claim {path.name}: {ex}")
        return
    say(f"running {path.name} -> {run_dir.relative_to(POC_DIR)}")
    started = time.time()
    try:
        source = script.read_text(encoding="utf-8")
        code = compile(source, str(script), "exec")
        scope: dict[str, Any] = {"__name__": "__main__", "__file__": str(script), "__builtins__": __builtins__}
        exec(code, scope)  # noqa: S102 - research tooling, files are ours
    except Exception:  # noqa: BLE001
        message = f"{path.name} failed:\n{traceback.format_exc()[-2000:]}"
        say(message)
        with (run_dir / "probe_output.txt").open("a", encoding="utf-8") as f:
            f.write(message + "\n")
    finally:
        say(f"finished {path.name} in {time.time() - started:.1f} s")


def on_tick(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    global _last_poll
    now = time.monotonic()
    if now - _last_poll < POLL_INTERVAL_S:
        return
    _last_poll = now
    try:
        # Only files that stopped changing: a file still being written would run empty.
        pending = sorted(path for path in INBOX.glob("*.py") if time.time() - path.stat().st_mtime >= SETTLE_S)
    except OSError:
        return
    for path in pending[:1]:
        run_probe(path)


INBOX.mkdir(parents=True, exist_ok=True)
RUNS.mkdir(parents=True, exist_ok=True)
if unrealsdk.hooks.has_hook(TICK_FUNCTION, Type.POST, HOOK_ID):
    unrealsdk.hooks.remove_hook(TICK_FUNCTION, Type.POST, HOOK_ID)
unrealsdk.hooks.add_hook(TICK_FUNCTION, Type.POST, HOOK_ID, on_tick)
say(f"{time.strftime('%H:%M:%S')} probe runner installed; watching {INBOX}")
