"""Account profiles: ~/.meltwater/profiles.json (chmod 600). API keys are never printed.

File shape:
  {"default": "acme",
   "profiles": {"acme": {"api_key": "...", "company_id": "...", "template": null, "languages": ["en"]}}}
"""
import json
import os
import stat
import sys

from .errors import MwError


def profiles_path():
    return os.environ.get("MWPULL_PROFILES") or os.path.join(os.path.expanduser("~"), ".meltwater", "profiles.json")


def load(path=None):
    path = path or profiles_path()
    if not os.path.exists(path):
        return {"default": None, "profiles": {}}
    if os.name != "nt" and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"warning: {path} is readable by others; run: chmod 600 '{path}'", file=sys.stderr)
    with open(path) as f:
        data = json.load(f)
    data.setdefault("profiles", {})
    data.setdefault("default", None)
    return data


def save(data, path=None):
    path = path or profiles_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def resolve(name=None, path=None):
    """Return {name, key, api_key, company_id, template, languages, limits}. Falls back to MELTWATER_* env vars.
    `key` identifies the usage ledger: Meltwater's limits belong to the company, shared by every API key in it, so budgets are tracked per company, not per profile."""
    data = load(path)
    name = name or data.get("default")
    if name:
        p = data["profiles"].get(name)
        if not p:
            known = ", ".join(data["profiles"]) or "none"
            raise MwError(f"No profile '{name}' in {path or profiles_path()} (known: {known}). "
                          f"Add one with: mwpull profile add {name}")
        if not p.get("api_key") or not p.get("company_id"):
            raise MwError(f"Profile '{name}' needs api_key and company_id")
        return {"name": name, "key": f"company-{p['company_id']}", "api_key": p["api_key"], "company_id": str(p["company_id"]),
                "template": p.get("template"), "languages": p.get("languages"), "limits": p.get("limits") or {}}
    key, cid = os.environ.get("MELTWATER_API_KEY"), os.environ.get("MELTWATER_COMPANY_ID")
    if key and cid:
        return {"name": None, "key": f"company-{cid}", "api_key": key, "company_id": cid,
                "template": os.environ.get("MELTWATER_TEMPLATE"), "languages": None, "limits": {}}
    raise MwError("No account selected. Use --profile NAME (create one with `mwpull profile add NAME`), "
                  "or set MELTWATER_API_KEY and MELTWATER_COMPANY_ID.")
