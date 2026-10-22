"""User defaults: ~/.meltwater/config.json (next to profiles.json). No secrets live here.

Keys:  out_dir  parent folder for run folders (stored as an absolute path)
       format   csv | jsonl
Precedence for each: command-line flag > environment (MWPULL_OUT_DIR) > this file > built-in default.
"""
import json
import os

from .client import MwError
from .profiles import profiles_path

KEYS = {"out_dir": "parent folder for run folders (default ./out)",
        "format": "default output format: csv or jsonl (default csv)"}


def config_path():
    return os.path.join(os.path.dirname(profiles_path()), "config.json")


def load():
    try:
        with open(config_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError:
        raise MwError(f"{config_path()} isn't valid JSON; fix or delete it")


def save(data):
    path = config_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def normalize(key, value):
    if key not in KEYS:
        raise MwError(f"Unknown setting '{key}'. Available: {', '.join(KEYS)}")
    if key == "out_dir":
        return os.path.abspath(os.path.expanduser(value))
    if value.lower() not in ("csv", "jsonl"):
        raise MwError("format must be csv or jsonl")
    return value.lower()


def out_dir_default():
    return os.environ.get("MWPULL_OUT_DIR") or load().get("out_dir") or "out"


def format_default():
    return load().get("format") or "csv"
