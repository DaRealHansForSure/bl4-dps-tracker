"""Fallback discovery: log EVERY unreal function call to unrealsdk.log. Fire one shot, then run logall_off.py."""
from unrealsdk import hooks, logging
hooks.log_all_calls(True)
logging.info("[dmgprobe] log_all_calls ON - fire one shot now, then `pyexec logall_off.py`")
