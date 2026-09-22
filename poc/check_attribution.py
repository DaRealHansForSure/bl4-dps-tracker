"""Find misattribution in a session log, by the shape it leaves behind.

One stream of damage (one source hitting one target with one damage type, tick after tick)
must not change owner in the middle. When it does, the windows guessed wrong: that is how
the grenade's corrosive area ended up as Skill on 2026-09-22. This reads a session log and
prints every such flip, with the rule that claimed each side (`why`, written since 1.4).

    python poc/check_attribution.py                  the newest session
    python poc/check_attribution.py <file.jsonl>     one session
    python poc/check_attribution.py --all            every kept session

Research tooling; it reads logs and changes nothing.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

LOG_DIR = Path.home() / "AppData" / "Local" / "BL4DamageTracker" / "logs"
SESSION_GLOB = "session-*.jsonl"
# Two hits of one target and one damage type this close together are one stream.
STREAM_GAP_S = 3.0
# A flip this soon after the stream started is a slow first attribution, not a takeover.
MIN_STREAK_BEFORE_FLIP = 2


def read_hits(path: Path) -> list[dict]:
    hits = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith('{"t"') or '"dmg"' not in line:
            continue
        try:
            hits.append(json.loads(line))
        except ValueError:
            continue
    return hits


def flips(hits: list[dict]) -> list[dict]:
    """Every point where one target's stream of one damage type changed source."""
    streams: dict[tuple[str, str], dict] = {}
    found = []
    for hit in sorted(hits, key=lambda hit: float(hit["t"])):
        target = str(hit.get("target", ""))
        if not target:
            continue
        key = (target, str(hit.get("dtype", "")))
        source = str(hit.get("weapon", "")) + "/" + str(hit.get("src", ""))
        now = float(hit["t"])
        stream = streams.get(key)
        if stream is None or now - stream["last"] > STREAM_GAP_S:
            streams[key] = {"source": source, "last": now, "count": 1, "since": now, "why": hit.get("why")}
            continue
        if source != stream["source"] and stream["count"] >= MIN_STREAK_BEFORE_FLIP:
            found.append({
                "t": now,
                "target": target,
                "dtype": key[1],
                "from": stream["source"],
                "from_why": stream["why"],
                "to": source,
                "to_why": hit.get("why"),
                "after": now - stream["since"],
                "hits": stream["count"],
                "dmg": float(hit["dmg"]),
            })
        if source != stream["source"]:
            streams[key] = {"source": source, "last": now, "count": 1, "since": now, "why": hit.get("why")}
        else:
            stream["last"] = now
            stream["count"] += 1
            stream["why"] = hit.get("why")
    return found


def report(path: Path) -> int:
    hits = read_hits(path)
    found = flips(hits)
    print(f"{path.name}: {len(hits)} hits, {len(found)} stream(s) that changed owner")
    for flip in found:
        when = time.strftime("%H:%M:%S", time.localtime(flip["t"]))
        print(f"   {when} {flip['target'][:34]:<34} {flip['dtype']:<18} "
              f"{flip['from']} ({flip['from_why']}) -> {flip['to']} ({flip['to_why']}) "
              f"after {flip['after']:.1f} s and {flip['hits']} hit(s), {flip['dmg']:.1f} damage")
    return len(found)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--all":
        paths = sorted(LOG_DIR.glob(SESSION_GLOB))
    elif argv:
        paths = [Path(argv[0])]
    else:
        paths = sorted(LOG_DIR.glob(SESSION_GLOB))[-1:]
    if not paths:
        print(f"no session logs in {LOG_DIR}")
        return 1
    total = 0
    for path in paths:
        total += report(path)
    if len(paths) > 1:
        print(f"{total} flip(s) over {len(paths)} session(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
