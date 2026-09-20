"""Fallback discovery: log EVERY unreal function call to Plugins/unrealsdk.calls.tsv."""
from unrealsdk import hooks, logging

hooks.log_all_calls(True)
logging.info("[dmgprobe] log_all_calls ON - fire ONE shot now, then `pyexec logall_off.py`")
