"""
Phase 0 probe #19: the UI's item card model.

The inventory screen is Coherent HTML (`menu_equip.html`), fed by native data collectors.
The Cohtml plugin binds *reflected* UObjects and structs to the page's data models
(`CohtmlBaseComponent.UpdateWholeDataModelFromObject/FromStruct`), so the item card's name
must sit in a reflected model object or struct somewhere, even if the collector class
itself shows no text property. This probe:

1. lists every class and script struct of the game's UI modules whose name says model,
   card, item data, tooltip, inspect, equip or backpack, with all properties and types;
2. dumps the live instances of those classes (up to three each) with every string, text
   and name property's value, so a "Lionized Parasite" shows up next to its property;
3. lists the full function and property sets of the Cohtml component classes and of every
   OakUIDataCollector class.

    pyexec probe_names4.py    with the inventory screen open on a gun, then once more closed

Reads object properties only; no hooks, no calls.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import logging
from unrealsdk.unreal import UFunction, UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
MODULES = ("/Script/OakGame", "/Script/GbxUICoherent", "/Script/GbxUI.", "/Script/GbxUIUMG", "/Script/CohtmlPlugin", "/Script/GbxGame")
NAME_PATTERN = re.compile(r"Model|Card|ItemData|ItemInfo|UIData|Tooltip|Inspect|Equip|Backpack|Loadout|ItemSlot|Compare|Vending|Inventory", re.IGNORECASE)
TEXT_KINDS = ("TextProperty", "StrProperty", "NameProperty")
DUMP_CLASSES = re.compile(r"^(CohtmlBaseComponent|CohtmlWidget|CohtmlBlueprintFunctionLibrary|OakUIBaseDataCollector|OakUIDataCollector_.*|GbxUIDataCollector.*|GbxUICoherent.*|NexusBPLibrary|GameDataHandleBlueprintLibrary|OakUIFunctionLibrary|GbxDefPtrLibrary.*|.*DefPtr.*Library)$")
MAX_INSTANCES = 3
MAX_ITEMS = 8
MAX_VALUE_CHARS = 200


def say(msg: str) -> None:
    logging.info(f"[dmgnames4] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def type_text(prop: Any) -> str:
    kind = str(prop.Class.Name).replace("Property", "")
    try:
        if kind == "Struct":
            return f"Struct<{prop.Struct.Name}>"
        if kind == "Object":
            return f"Object<{prop.PropertyClass.Name}>"
        if kind == "Array":
            return f"Array<{type_text(prop.Inner)}>"
    except Exception:  # noqa: BLE001
        pass
    return kind


def described(value: Any, depth: int = 0) -> str:
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, WrappedStruct):
            if depth >= 2:
                return f"<struct {value._type.Name}>"
            parts = []
            for prop in value._type._properties():
                name = str(prop.Name)
                try:
                    parts.append(f"{name}={described(getattr(value, name), depth + 1)}")
                except Exception as ex:  # noqa: BLE001
                    parts.append(f"{name}=<err {type(ex).__name__}>")
            return "{" + ", ".join(parts) + "}"
        if isinstance(value, WrappedArray):
            items = [described(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
            more = "" if len(value) <= MAX_ITEMS else f", ... {len(value)} items"
            return "[" + ", ".join(items) + more + "]"
        text = repr(value)
        return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"


def in_modules(path: str) -> bool:
    return any(path.startswith(module) for module in MODULES)


def owned_by(prop: Any, cls: Any) -> bool:
    """Whether a property is declared on this class or struct itself (properties are FFields
    with an Owner, not UObjects with an Outer)."""
    try:
        owner = prop.Owner
        return isinstance(owner, UObject) and int(owner._get_address()) == int(cls._get_address())
    except Exception:  # noqa: BLE001
        return False


def own_functions(cls: Any) -> list[UFunction]:
    return [field for field in cls._fields() if isinstance(field, UFunction) and int(field.Outer._get_address()) == int(cls._get_address())]


def signature(func: UFunction) -> str:
    return ", ".join(f"{prop.Name}:{type_text(prop)}" for prop in func._properties())


def has_text_in_tree(struct: Any, depth: int = 0) -> bool:
    """Whether a struct (or its nested structs, two levels) has a text-like field."""
    for prop in struct._properties():
        kind = str(prop.Class.Name)
        if kind in TEXT_KINDS:
            return True
        if kind == "StructProperty" and depth < 2:
            try:
                if has_text_in_tree(prop.Struct, depth + 1):
                    return True
            except Exception:  # noqa: BLE001
                pass
    return False


def list_model_types() -> None:
    say("step 1/3: model-like classes and structs of the UI modules")
    for struct in sorted(unrealsdk.find_all("ScriptStruct", exact=True), key=lambda item: str(item._path_name())):
        path = str(struct._path_name())
        if not in_modules(path) or not NAME_PATTERN.search(str(struct.Name)):
            continue
        props = [f"{prop.Name}:{type_text(prop)}" for prop in struct._properties()]
        marker = " (has text)" if has_text_in_tree(struct) else ""
        say(f"   struct {path}{marker}: {', '.join(props)[:1200]}")
    for cls in sorted(unrealsdk.find_all("Class", exact=True), key=lambda item: str(item._path_name())):
        path = str(cls._path_name())
        if not in_modules(path) or not NAME_PATTERN.search(str(cls.Name)):
            continue
        props = [f"{prop.Name}:{type_text(prop)}" for prop in cls._properties() if owned_by(prop, cls)]
        instances = len(list(unrealsdk.find_all(str(cls.Name), exact=True)))
        say(f"   class {path} ({instances} instance(s)): {', '.join(props)[:1200]}")


def dump_model_instances() -> None:
    say("step 2/3: text-like values on the live instances of those classes")
    for cls in sorted(unrealsdk.find_all("Class", exact=True), key=lambda item: str(item._path_name())):
        path = str(cls._path_name())
        if not in_modules(path) or not NAME_PATTERN.search(str(cls.Name)):
            continue
        text_props = [prop for prop in cls._properties() if str(prop.Class.Name) in TEXT_KINDS or str(prop.Class.Name) == "StructProperty"]
        if not text_props:
            continue
        for obj in list(unrealsdk.find_all(str(cls.Name), exact=True))[:MAX_INSTANCES]:
            values = []
            for prop in text_props:
                name = str(prop.Name)
                try:
                    value = getattr(obj, name)
                except Exception:  # noqa: BLE001
                    continue
                text = described(value)
                if str(prop.Class.Name) == "StructProperty" and not re.search(r"[A-Za-z]{4}", text.replace(name, "")):
                    continue
                values.append(f"{name}={text}")
            if values:
                say(f"   {obj._path_name()}: {' | '.join(values)[:1500]}")


def dump_ui_plumbing() -> None:
    say("step 3/3: Cohtml components, data collectors and the Nexus/def-pointer libraries, everything they have")
    for struct_name in ("GbxDefPtr", "GameDataHandle", "NexusConfigDataBP"):
        try:
            struct = unrealsdk.find_object("ScriptStruct", f"/Script/GbxNexus.{struct_name}")
        except ValueError:
            try:
                struct = unrealsdk.find_object("ScriptStruct", f"/Script/GbxEngine.{struct_name}")
            except ValueError:
                say(f"   struct {struct_name}: not found under GbxNexus or GbxEngine")
                continue
        say(f"   struct {struct._path_name()} ({struct._get_struct_size()} bytes): " + ", ".join(f"{prop.Name}:{type_text(prop)}" for prop in struct._properties()))
    for cls in sorted(unrealsdk.find_all("Class", exact=True), key=lambda item: str(item._path_name())):
        if not DUMP_CLASSES.match(str(cls.Name)):
            continue
        say(f"   {cls._path_name()} (super {cls.SuperField.Name if cls.SuperField is not None else '-'})")
        for prop in cls._properties():
            if owned_by(prop, cls):
                say(f"      prop {prop.Name}: {type_text(prop)}")
        for func in own_functions(cls):
            say(f"      fn {func.Name}({signature(func)})")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v4")
for step in (list_model_types, dump_model_instances, dump_ui_plumbing):
    try:
        step()
    except Exception as ex:  # noqa: BLE001
        say(f"   step failed: {type(ex).__name__}: {ex}")
say("done")
