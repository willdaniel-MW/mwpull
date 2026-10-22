"""Best-effort 'is a newer mwpull available?' check, based on the repo's vX.Y.Z git tags.

Uses `git ls-remote` so it needs nothing beyond git itself (the repo is public).
Never blocks or fails a run: short timeout, silent on any error, result cached on disk.
"""
import json
import os
import re
import subprocess
import time

from . import __version__, profiles

REPO_URL = "https://github.com/willdaniel-MW/mwpull.git"
OK_TTL = 24 * 3600     # re-check at most daily
FAIL_TTL = 3600        # after a failed check (offline, no access), wait an hour
TIMEOUT = 3
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(s):
    m = _TAG.match(s.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def latest_from_tags(tags):
    versions = [(v, t) for t in tags if (v := parse_version(t))]
    return max(versions)[1].lstrip("v") if versions else None


def fetch_latest(timeout=TIMEOUT):
    """Highest vX.Y.Z tag on the remote, or None on any failure."""
    try:
        r = subprocess.run(["git", "ls-remote", "--tags", "--refs", REPO_URL], capture_output=True, text=True,
                           timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode != 0:
            return None
        tags = [line.split("refs/tags/", 1)[1] for line in r.stdout.splitlines() if "refs/tags/" in line]
        return latest_from_tags(tags)
    except Exception:
        return None


def cache_path():
    return os.path.join(os.path.dirname(profiles.profiles_path()), "update-check.json")


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _write(path, data):
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def latest_version(force=False, fetch=fetch_latest, now=None, path=None):
    """Latest known version string (or None). Uses the cache unless it is stale or force=True."""
    path, now = path or cache_path(), now if now is not None else time.time()
    c = _read(path)
    ttl = OK_TTL if c.get("ok") else FAIL_TTL
    if not force and c.get("checked_at") and now - c["checked_at"] < ttl:
        return c.get("latest")
    latest = fetch()
    _write(path, {"checked_at": now, "ok": latest is not None, "latest": latest or c.get("latest")})
    return latest or c.get("latest")


def is_newer(latest, current):
    a, b = parse_version(latest or ""), parse_version(current or "")
    return bool(a and b and a > b)


def upgrade_command(latest):
    return f"pipx install --force git+{REPO_URL}@v{latest}"


def notice(current=__version__, **kw):
    """One-line update notice, or None if up to date / unknown."""
    if os.environ.get("MWPULL_NO_UPDATE_CHECK"):
        return None
    latest = latest_version(**kw)
    if is_newer(latest, current):
        return f"mwpull {latest} is available (you have {current}). Update: {upgrade_command(latest)}"
    return None
