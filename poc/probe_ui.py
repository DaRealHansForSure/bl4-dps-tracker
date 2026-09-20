"""
Phase 0 probe #10: can the mod draw numbers on screen from Python?

BL4's HUD is Coherent UI; a call trace shows `HUD:ReceiveDrawHUD` never fires, so canvas
text is out. This probe tries the UMG route instead: create a UserWidget through
`WidgetBlueprintLibrary.Create`, give it a TextBlock as root, add it to the viewport and
set the text. `UserWidget` itself is abstract, so a concrete native subclass from the game
serves as the container.

    pyexec probe_ui.py        create the widget and show "DMG 12,345" near the top left
    pyexec probe_ui_off.py    remove it again

Prints to the console and to probe_output.txt.
"""

from __future__ import annotations

import builtins
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
MAX_CREATE_ATTEMPTS = 8
PREFERRED_CONTAINER_CLASSES = ("GbxUIUMGTickWidget", "OakUIUMGWidget_HUDItemBase", "GbxUIUMGListViewItemWidget")
MAX_LISTED_CLASSES = 40
Z_ORDER_ABOVE_HUD = 10000
SAMPLE_TEXT = "DMG 12,345   avg/s 1,234"
SCREEN_POSITION = (120.0, 320.0)


def say(msg: str) -> None:
    logging.info(f"[dmgui] {msg}")
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
    return repr(value)[:300]


def native_user_widget_classes() -> list[Any]:
    """Native UserWidget subclasses, preferred ones first. Abstract ones cannot be filtered
    out here (pyunrealsdk does not expose ClassFlags); Create() returns None for them."""
    user_widget = unrealsdk.find_class("UserWidget")
    candidates = []
    for cls in unrealsdk.find_all("Class", exact=True):
        if cls is user_widget or not cls._inherits(user_widget):
            continue
        if not str(cls.Outer.Name).startswith("/Script/"):
            continue
        candidates.append(cls)
    preferred = [c for c in candidates if str(c.Name) in PREFERRED_CONTAINER_CLASSES]
    others = [c for c in candidates if str(c.Name) not in PREFERRED_CONTAINER_CLASSES]
    return preferred + others


def create_text_widget(pc: UObject, container_class: Any) -> UObject:
    library = unrealsdk.find_class("WidgetBlueprintLibrary").ClassDefaultObject
    widget = library.Create(pc, container_class, pc)
    if widget is None:
        raise RuntimeError(f"Create returned None for {container_class.Name}")
    say(f"   created {widget._path_name()}")
    tree = widget.WidgetTree
    say(f"   WidgetTree: {described(lambda: tree)}, root before: {described(lambda: tree.RootWidget)}")
    text_block = unrealsdk.construct_object("TextBlock", tree, "DmgTrackerText")
    tree.RootWidget = text_block
    text_block.SetText(SAMPLE_TEXT)
    return widget


def show_on_screen(widget: UObject) -> None:
    widget.AddToViewport(Z_ORDER_ABOVE_HUD)
    say(f"   AddToViewport done, IsInViewport: {described(lambda: widget.IsInViewport())}")
    position = unrealsdk.make_struct("Vector2D", X=SCREEN_POSITION[0], Y=SCREEN_POSITION[1])
    say(f"   SetPositionInViewport: {described(lambda: widget.SetPositionInViewport(position, True))}")
    say(f"   visibility: {described(lambda: widget.GetVisibility())}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} ui probe")
pc = get_pc()
say(f"   MyHUD: {described(lambda: pc.MyHUD)}")

old_widget = getattr(builtins, "_dmgui_widget", None)
if old_widget is not None:
    say(f"   removing widget from an earlier run: {described(lambda: old_widget.RemoveFromParent())}")
    builtins._dmgui_widget = None  # type: ignore[attr-defined]

say("step 1/3: native UserWidget classes")
candidates = native_user_widget_classes()
for cls in candidates[:MAX_LISTED_CLASSES]:
    say(f"   {cls._path_name()}")
say(f"   {len(candidates)} candidate(s)")

say("step 2/3: create widget + TextBlock")
widget = None
for cls in candidates[:MAX_CREATE_ATTEMPTS]:
    try:
        widget = create_text_widget(pc, cls)
        say(f"   container class used: {cls.Name}")
        break
    except Exception as ex:  # noqa: BLE001
        say(f"   {cls.Name}: {type(ex).__name__}: {ex}")

say("step 3/3: add to viewport")
if widget is None:
    say("   no container class worked")
else:
    builtins._dmgui_widget = widget  # type: ignore[attr-defined]
    try:
        show_on_screen(widget)
        say(f"   look for '{SAMPLE_TEXT}' on screen, then `pyexec probe_ui_off.py`")
    except Exception as ex:  # noqa: BLE001
        say(f"   viewport: {type(ex).__name__}: {ex}")
