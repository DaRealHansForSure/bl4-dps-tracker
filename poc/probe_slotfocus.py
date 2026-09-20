"""
Phase 0 probe #8: what does the inventory screen receive when a weapon slot is hovered?

A call trace over a weapon swap showed no reflected function at all, but hovering a slot
in the inventory fires `ui_script_backpack_C:WeaponSlotFocused`. This probe hooks it and
prints every argument, and if an argument is an `InventoryItemSelectionData` struct, also
what `InventoryItemBlueprintLibrary.GetSummary_InventoryItemSelectionData` says about it.

    pyexec probe_slotfocus.py    install the hook, then open the inventory and hover weapons
    pyexec probe_stop.py         remove it (also removes the other probe hooks)

Prints to the console and to probe_output.txt.
"""

from __future__ import annotations

import builtins
import time
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import hooks, logging
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "dmgslotfocus"
HOOKED_FUNCTIONS = (
    "/Game/UI/Scripts/ui_script_backpack.ui_script_backpack_C:WeaponSlotFocused",
    "/Game/UI/Scripts/ui_script_backpack.ui_script_backpack_C:BackpackSlotUnfocused",
)
SELECTION_STRUCT_NAME = "InventoryItemSelectionData"
MAX_VALUE_CHARS = 300


def say(msg: str) -> None:
    logging.info(f"[dmgslotfocus] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def described(fn) -> str:
    """Run fn and return its result as a string, or the error text instead of raising."""
    try:
        value = fn()
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"
    if isinstance(value, UObject):
        return f"{value._path_name()} ({value.Class.Name})"
    return repr(value)[:MAX_VALUE_CHARS]


def dump_struct_definition(struct_name: str) -> None:
    """Print the fields of the first ScriptStruct with this name; the module is unknown."""
    for struct in unrealsdk.find_all("ScriptStruct", exact=True):
        if str(struct.Name) != struct_name:
            continue
        say(f"   {struct._path_name()} (size {struct.PropertySize})")
        for prop in struct._properties():
            say(f"      {prop.Name}: {prop.Class.Name}")
        return
    say(f"   {struct_name}: no such struct")


def summarize_selection(value: Any) -> str:
    library = unrealsdk.find_class("InventoryItemBlueprintLibrary").ClassDefaultObject
    return described(lambda: library.GetSummary_InventoryItemSelectionData(value))


def on_slot_event(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    say(f"{time.strftime('%H:%M:%S')} {func.func.Name} on {described(lambda: obj)}")
    for prop in args._type._properties():
        prop_name = str(prop.Name)
        say(f"   {prop_name}: {prop.Class.Name} = {described(lambda: getattr(args, prop_name))}")
        if str(prop.Class.Name) != "StructProperty":
            continue
        try:
            value = getattr(args, prop_name)
            struct_name = str(value._type.Name)
            say(f"      struct type: {value._type._path_name()}")
        except Exception:  # noqa: BLE001
            continue
        if struct_name == SELECTION_STRUCT_NAME:
            say(f"      summary: {summarize_selection(value)}")


# Re-running the probe must not stack hooks, so drop any earlier registration first.
for function_path in HOOKED_FUNCTIONS:
    if hooks.has_hook(function_path, hooks.Type.PRE, HOOK_ID):
        hooks.remove_hook(function_path, hooks.Type.PRE, HOOK_ID)

say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} slot focus probe")
say("InventoryItemSelectionData definition:")
dump_struct_definition(SELECTION_STRUCT_NAME)

for function_path in HOOKED_FUNCTIONS:
    hooks.add_hook(function_path, hooks.Type.PRE, HOOK_ID, on_slot_event)
    say(f"   hooked {function_path}")

# Let probe_stop.py know what to remove.
builtins._dmgslotfocus_hooks = list(HOOKED_FUNCTIONS)  # type: ignore[attr-defined]
say("open the inventory and hover the weapon slots, then `pyexec probe_stop.py`")
