"""Fallback discovery: log EVERY unreal function call to Plugins/unrealsdk.calls.tsv."""
from unrealsdk import hooks, logging

hooks.log_all_calls(False)
logging.info("[dmgprobe] log_all_calls OFF - trace written to Plugins/unrealsdk.calls.tsv")
