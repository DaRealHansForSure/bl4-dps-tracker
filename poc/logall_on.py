"""Fallback discovery: log EVERY unreal function call to Plugins/unrealsdk.calls.tsv.

Records the trace file size at switch-on in probe_output.txt, so a slice between an on/off
pair can be cut out afterwards (the SDK appends to the same file across runs).
"""

import time
from pathlib import Path

from unrealsdk import hooks, logging

OUT = Path(__file__).parent / "probe_output.txt"
TRACE = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4\OakGame\Binaries\Win64\Plugins\unrealsdk.calls.tsv")

size = TRACE.stat().st_size if TRACE.exists() else 0
with OUT.open("a", encoding="utf-8") as f:
    f.write(f"{time.strftime('%H:%M:%S')} logall ON at byte {size}\n")
hooks.log_all_calls(True)
logging.info(f"[dmgprobe] log_all_calls ON (trace at byte {size}) - do ONE thing now, then `pyexec logall_off.py`")
