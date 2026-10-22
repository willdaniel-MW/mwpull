"""Usage limits: per-profile budgets, pacing, and Meltwater's RateLimit-* response headers.

Meltwater's documented limits (https://developer.meltwater.com/guides/getting-started/usage-limits) that matter here:
  earned-media search     5 calls/second and 100 calls/minute
  analytics calls / day   50 (inclusive access) or per contract; search counts; RateLimit-Day-* headers
  documents / month       30,000 (inclusive) or per contract; shared by exports and searches within a company
  exports / month         50 (inclusive) or per contract; 20 export creations/minute
Your account's real numbers may differ (contracts), so each profile can set its own; headers win when present.

State lives in ~/.meltwater/usage.json (per profile): calls in the current day window, documents this month,
and any 'blocked until' time learned from the server. mwpull never starts a call it knows will be refused.
"""
import json
import os
import time
from datetime import datetime, timezone

from .errors import MwError
from .profiles import profiles_path

DEFAULT_CALLS_PER_MINUTE = 90  # headroom under the documented 100/min for search
HOUR_RESERVE = 3               # stop proactively when this few calls remain in the hourly window
LIMIT_KEYS = ("calls_per_minute", "calls_per_day", "docs_per_month")


def ledger_path():
    return os.path.join(os.path.dirname(profiles_path()), "usage.json")


def fmt_when(ts, now=None):
    """'Fri Oct 16 11:42 PM EDT (in 4h 12m)'"""
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(ts).astimezone().strftime("%a %b %d %I:%M %p %Z")
    secs = max(0, int(ts - now))
    h, m = divmod(secs // 60, 60)
    return f"{local} (in {h}h {m:02d}m)" if h else f"{local} (in {m}m)"


class LimitReached(MwError):
    """A usage limit (daily calls, monthly documents, ...) stopped the run. Progress is saved; resume later."""

    def __init__(self, kind, reset=None, run_dir=None):
        self.kind, self.reset, self.run_dir = kind, reset, run_dir
        super().__init__(self.describe())

    def describe(self):
        when = f"Resets {fmt_when(self.reset)}." if self.reset else \
            "Meltwater didn't say when it resets; check `mwpull usage --remote` or ask your account manager."
        return f"{self.kind} reached. {when}"


def _hget(headers, name):
    try:
        v = headers.get(name)
        if v is None:  # plain dicts are case-sensitive
            v = next((val for k, val in headers.items() if k.lower() == name.lower()), None)
    except AttributeError:
        return None
    return v


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _abs_reset(v, now):
    """Reset headers are *seconds from now* in practice (e.g. 78049); Meltwater's docs say Unix timestamp.
    Accept both: anything that looks like an epoch time is absolute, anything smaller is relative."""
    if v is None:
        return None
    return v if v > 1_000_000_000 else int(now + v)


def parse_headers(headers, now=None):
    """RateLimit-Day-* / RateLimit-Minute-* (and the per-hour RateLimit-* / X-RateLimit-*-Hour) headers
    -> dict of ints (None when absent). *_reset values are absolute Unix timestamps."""
    now = time.time() if now is None else now
    out = {}
    for scope in ("Day", "Minute"):
        for field in ("Limit", "Remaining", "Reset"):
            out[f"{scope.lower()}_{field.lower()}"] = _int(_hget(headers, f"RateLimit-{scope}-{field}"))
    # NB: the generic RateLimit-Limit/Remaining/Reset mean "per hour" on search/count responses but "per minute"
    # on the usage endpoints, so the hour values are only taken from the explicit per-hour headers.
    out["hour_limit"] = _int(_hget(headers, "X-RateLimit-Limit-Hour"))
    out["hour_remaining"] = _int(_hget(headers, "X-RateLimit-Remaining-Hour"))
    has_hour = out["hour_remaining"] is not None
    out["hour_reset"] = _int(_hget(headers, "RateLimit-Reset")) if has_hour else None
    if out["minute_limit"] is None:
        out["minute_limit"] = _int(_hget(headers, "X-RateLimit-Limit-Minute"))
    if out["minute_remaining"] is None:
        out["minute_remaining"] = _int(_hget(headers, "X-RateLimit-Remaining-Minute"))
    for k in ("day_reset", "minute_reset", "hour_reset"):
        out[k] = _abs_reset(out[k], now)
    return out


def month_end(now):
    d = datetime.fromtimestamp(now, timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc).timestamp()


class UsageGuard:
    def __init__(self, key, limits=None, path=None, clock=time.time):
        self.key, self.limits, self.clock = key, {k: v for k, v in (limits or {}).items() if v}, clock
        self.path = path or ledger_path()
        self.d = self._read_all().get(key, {})
        self._last_call = 0.0
        self._roll(clock())

    @property
    def cpm(self):
        """Calls/minute we pace at: your setting (or 90), never above 90% of the limit Meltwater reported."""
        cfg = self.limits.get("calls_per_minute") or DEFAULT_CALLS_PER_MINUTE
        learned = self.d.get("minute_limit")
        return max(1, min(cfg, int(learned * 0.9))) if learned else cfg

    def knows_limits(self):
        """True once the daily allowance is known (configured, or learned from a response's headers)."""
        return bool(self.limits.get("calls_per_day") or self.d.get("day_limit"))

    # --- persistence ---------------------------------------------------------------------------------
    def _read_all(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            data = self._read_all()
            data[self.key] = self.d
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass  # bookkeeping must never break a run

    # --- windows -------------------------------------------------------------------------------------
    def _roll(self, now):
        d = self.d
        if d.get("day_reset") and now >= d["day_reset"]:
            for k in ("day_calls", "day_start", "day_reset", "day_remaining"):
                d.pop(k, None)
        if d.get("blocked_until") and now >= d["blocked_until"]:
            d.pop("blocked_until", None)
            d.pop("blocked_reason", None)
        ym = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
        if d.get("month") != ym:
            d["month"], d["month_docs"] = ym, 0

    def calls_per_day(self):
        cfg, learned = self.limits.get("calls_per_day"), self.d.get("day_limit")
        return min(x for x in (cfg, learned) if x) if (cfg or learned) else None

    # --- the three hooks the client calls ------------------------------------------------------------
    def before_call(self):
        """Raise LimitReached if a known limit is hit; otherwise return seconds to pause for pacing."""
        now = self.clock()
        self._roll(now)
        d = self.d
        if d.get("blocked_until"):
            raise LimitReached(d.get("blocked_reason", "usage limit"), d["blocked_until"])
        cpd = self.calls_per_day()
        if cpd and d.get("day_calls", 0) >= cpd:
            raise LimitReached(f"Daily call limit ({cpd} calls)", d.get("day_reset"))
        hr, hres = d.get("hour_remaining"), d.get("hour_reset")
        if hr is not None and hr <= HOUR_RESERVE and hres and hres > now:
            raise LimitReached("Hourly call limit (stopping just short of it)", hres)
        dpm = self.limits.get("docs_per_month")
        if dpm and d.get("month_docs", 0) >= dpm:
            raise LimitReached(f"Monthly document limit ({dpm} documents)", month_end(now))
        wait = max(0.0, self._last_call + 60.0 / self.cpm - now)
        mr, mres = d.get("min_remaining"), d.get("min_reset")
        if mr is not None and mr <= 1 and mres and mres > now and now - d.get("min_seen", 0) < 90:
            wait = max(wait, min(mres - now + 0.5, 65.0))  # let the minute window refill instead of tripping a 429
        return wait

    def after_call(self, headers=None):
        now = self.clock()
        self._roll(now)
        d, info = self.d, parse_headers(headers or {}, now)
        d["day_calls"] = d.get("day_calls", 0) + 1
        if not d.get("day_start"):
            d["day_start"], d["day_reset"] = now, now + 86400
        if info["day_limit"]:
            d["day_limit"] = info["day_limit"]
        if info["day_remaining"] is not None:
            d["day_remaining"], d["server_seen"] = info["day_remaining"], now
            if info["day_limit"]:
                d["day_calls"] = info["day_limit"] - info["day_remaining"]
        if info["day_reset"]:
            d["day_reset"] = info["day_reset"]
        if info["minute_limit"]:
            d["minute_limit"] = info["minute_limit"]
        if info["minute_remaining"] is not None:
            d["min_remaining"], d["min_reset"], d["min_seen"] = info["minute_remaining"], info["minute_reset"], now
        if info["hour_limit"]:
            d["hour_limit"] = info["hour_limit"]
        if info["hour_remaining"] is not None:
            d["hour_remaining"], d["hour_reset"] = info["hour_remaining"], info["hour_reset"]
        if info["day_remaining"] == 0 and d.get("day_reset"):
            d["blocked_until"], d["blocked_reason"] = d["day_reset"], "Daily call limit"
        elif info["hour_remaining"] == 0 and info["hour_reset"]:
            d["blocked_until"], d["blocked_reason"] = info["hour_reset"], "Hourly call limit"
        self._last_call = now
        self._save()

    def add_docs(self, n):
        self.d["month_docs"] = self.d.get("month_docs", 0) + n
        self._save()

    def mark_blocked(self, kind, reset):
        if reset:
            self.d["blocked_until"], self.d["blocked_reason"] = reset, kind
            self.d["day_reset"] = reset if kind.startswith("Daily") else self.d.get("day_reset")
        self._save()

    # --- reporting -----------------------------------------------------------------------------------
    def status(self):
        now = self.clock()
        self._roll(now)
        d, cpd, dpm = self.d, self.calls_per_day(), self.limits.get("docs_per_month")
        return {"calls_per_minute": self.cpm, "calls_per_day": cpd, "docs_per_month": dpm,
                "day_calls": d.get("day_calls", 0), "day_reset": d.get("day_reset"),
                "day_remaining_server": d.get("day_remaining"), "day_limit_server": d.get("day_limit"),
                "calls_left_today": None if not cpd else max(0, cpd - d.get("day_calls", 0)),
                "month_docs": d.get("month_docs", 0),
                "docs_left_month": None if not dpm else max(0, dpm - d.get("month_docs", 0)),
                "blocked_until": d.get("blocked_until"), "blocked_reason": d.get("blocked_reason"),
                "minute_limit": d.get("minute_limit"), "hour_limit": d.get("hour_limit"),
                "hour_remaining": d.get("hour_remaining"), "hour_reset": d.get("hour_reset")}

    def warnings(self, est_calls, est_docs):
        """Plain-language heads-ups when a planned run won't fit the remaining budgets."""
        s, out = self.status(), []
        if s["calls_left_today"] is not None and est_calls > s["calls_left_today"]:
            out.append(f"needs ~{est_calls} API calls but only {s['calls_left_today']} are left in the current "
                       f"daily budget ({s['calls_per_day']}/day). The run will stop when it runs out"
                       + (f" (resets {fmt_when(s['day_reset'])})" if s["day_reset"] else "")
                       + "; continue then with `mwpull resume`, or pass --wait to continue automatically.")
        if s["docs_left_month"] is not None and est_docs > s["docs_left_month"]:
            out.append(f"would fetch ~{est_docs} documents but only {s['docs_left_month']} remain in this month's "
                       f"document budget ({s['docs_per_month']}/month, as counted by mwpull).")
        return out
