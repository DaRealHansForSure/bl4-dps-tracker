"""Removes the probe runner hook installed by probe_runner.py."""

from __future__ import annotations

import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type

TICK_FUNCTION = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
HOOK_ID = "dmgtracker-probe-runner"

if unrealsdk.hooks.has_hook(TICK_FUNCTION, Type.POST, HOOK_ID):
    unrealsdk.hooks.remove_hook(TICK_FUNCTION, Type.POST, HOOK_ID)
    logging.info("[probe-runner] removed")
else:
    logging.info("[probe-runner] was not installed")
