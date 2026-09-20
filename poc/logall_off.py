"""Fallback discovery: stop logging every unreal function call; records the trace size."""

import time
from pathlib import Path

from unrealsdk import hooks, logging

OUT = Path(__file__).parent / "probe_output.txt"
TRACE = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4\OakGame\Binaries\Win64\Plugins\unrealsdk.calls.tsv")

hooks.log_all_calls(False)
size = TRACE.stat().st_size if TRACE.exists() else 0
with OUT.open("a", encoding="utf-8") as f:
    f.write(f"{time.strftime('%H:%M:%S')} logall OFF at byte {size}\n")
logging.info(f"[dmgprobe] log_all_calls OFF (trace at byte {size})")
