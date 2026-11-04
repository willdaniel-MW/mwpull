"""Field profiles: named column sets, ~/.meltwater/fields.json (next to profiles.json), like account profiles.

File shape:  {"default": "slim", "profiles": {"slim": "document_id,title,url,sentiment"}}
A profile is a --fields spec (see `mwpull pull --help`). Built-in presets `all` and `basic` always exist.
Precedence: --fields SPEC > --fields-profile NAME > the default field profile > `all`.
"""
import json
import os

from .errors import MwError
from .output import PRESETS, parse_fields
from .profiles import profiles_path


def path():
    return os.path.join(os.path.dirname(profiles_path()), "fields.json")


def load():
    try:
        with open(path()) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except ValueError:
        raise MwError(f"{path()} isn't valid JSON; fix or delete it")
    data.setdefault("default", None)
    data.setdefault("profiles", {})
    return data


def save(data):
    p = path()
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)


def lookup(name, data=None):
    data = data or load()
    if name in data["profiles"]:
        return data["profiles"][name]
    if name in PRESETS:
        return name
    known = ", ".join(list(PRESETS) + list(data["profiles"]))
    raise MwError(f"No field profile '{name}' (known: {known}). Create one: mwpull fields add {name} --fields ...")


def resolve(fields=None, profile=None):
    """Return the concrete --fields spec string to use (recorded in the run's manifest)."""
    if fields:
        return fields
    data = load()
    if profile:
        return lookup(profile, data)
    if data["default"]:
        return lookup(data["default"], data)
    return "all"


def add(name, spec, set_default=False):
    if name in PRESETS:
        raise MwError(f"'{name}' is a built-in preset; pick another name")
    try:
        parse_fields(spec)
    except ValueError as e:
        raise MwError(str(e))
    data = load()
    data["profiles"][name] = spec
    if set_default:
        data["default"] = name
    save(data)


def remove(name):
    data = load()
    if name not in data["profiles"]:
        raise MwError(f"No field profile '{name}'")
    del data["profiles"][name]
    if data["default"] == name:
        data["default"] = None
    save(data)


def set_default(name):
    data = load()
    lookup(name, data)
    data["default"] = None if name == "all" else name
    save(data)
