"""Thin Meltwater v3 API client: search (paged) and count, with retry/backoff."""
import json
import sys
import time

import requests

from .errors import MwError  # noqa: F401  (re-exported: `from mwpull.client import MwError`)
from .limits import LimitReached, parse_headers

API = "https://api.meltwater.com/v3"
PAGE_SIZE = 100
MAX_PAGES = 10  # the search API caps a single query at 10 pages x 100 docs
QUERY_CAP = PAGE_SIZE * MAX_PAGES
MAX_DELAY = 10.0       # ceiling for the adaptive pause between calls
SPEED_UP_AFTER = 20    # consecutive good calls before easing the pause back toward the base delay


def iso(dt, z=False):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ("Z" if z else "")


class Client:
    def __init__(self, key, search_id, company_id, template=None, languages=("en",), delay=0.2, guard=None):
        self.s = requests.Session()
        self.s.headers.update({"apikey": key, "Accept": "application/json", "Content-Type": "application/json"})
        self.search_id, self.company_id = search_id, company_id
        self.template, self.languages = template, list(languages or [])
        self.base_delay = self.delay = delay
        self.guard = guard  # limits.UsageGuard or None
        self._ok_streak = 0

    def slow_down(self):
        """After a 429/5xx: double the pause between calls (so we stop re-triggering the limit)."""
        self._ok_streak = 0
        self.delay = min(max(self.delay * 2, 0.5), MAX_DELAY)

    def speed_up(self):
        """After a run of good calls: ease the pause back toward the configured base delay."""
        self._ok_streak += 1
        if self._ok_streak >= SPEED_UP_AFTER and self.delay > self.base_delay:
            self._ok_streak = 0
            self.delay = max(self.base_delay, self.delay * 0.75)

    def _request(self, method, url, counted=True, **kw):
        """`counted=False` for calls outside the search/analytics budget (the usage endpoints)."""
        g = self.guard if counted else None
        for attempt in range(8):
            wait = max(self.delay, g.before_call() if g else 0)  # may raise LimitReached before any call is made
            if wait:
                time.sleep(wait)
            try:
                r = self.s.request(method, url, timeout=120, **kw)
            except requests.RequestException as e:
                wait = min(2 ** attempt, 60)
                print(f"  network error ({e}); retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            info = parse_headers(r.headers)
            if g:
                g.after_call(r.headers)  # counts the call, learns RateLimit-* values, may block until reset
            if r.status_code in (429, 403) and info["day_remaining"] == 0:
                if g:
                    g.mark_blocked("Daily call limit", info["day_reset"])
                raise LimitReached("Daily call limit", info["day_reset"])
            if r.status_code == 429 and info["hour_remaining"] == 0 and info["hour_reset"]:
                if g:
                    g.mark_blocked("Hourly call limit", info["hour_reset"])
                raise LimitReached("Hourly call limit", info["hour_reset"])
            if r.status_code == 429 and info["minute_remaining"] == 0 and info["minute_reset"]:
                pause = max(1, info["minute_reset"] - time.time() + 1)
                print(f"  per-minute limit hit; waiting {pause:.0f}s for the window to reset", file=sys.stderr)
                time.sleep(pause)
                continue
            if r.status_code in (429, 502, 503, 504):
                wait = float(r.headers.get("Retry-After", min(2 ** attempt * 2, 120)))
                self.slow_down()
                print(f"  HTTP {r.status_code}; waiting {wait:.0f}s, pacing calls at {self.delay:.1f}s apart",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise MwError(f"HTTP {r.status_code}: check the API key / company ID for this profile (a 403 can "
                              f"also mean a usage limit; see `mwpull usage --remote`). {r.text[:200]}")
            if r.status_code == 404:
                raise MwError(f"HTTP 404: search {self.search_id} not found for company {self.company_id} "
                              f"(wrong search ID, or the search belongs to a different account).")
            if r.status_code >= 400:
                raise MwError(f"HTTP {r.status_code} from {url}: {r.text[:500]}")
            self.speed_up()
            return r.json()
        raise LimitReached("Rate limit (Meltwater kept refusing calls and didn't say why; this is often a daily or "
                           "monthly cap)")

    def search(self, start, end, page, page_size=PAGE_SIZE, order="asc", template="__default__"):
        """Return the list of documents on one page. `start`/`end` are datetimes (UTC, inclusive)."""
        body = {"start": iso(start), "end": iso(end), "tz": "UTC", "page": page, "page_size": page_size,
                "sort_by": "date", "sort_order": order}
        if self.languages:
            body["languages"] = self.languages
        tpl = self.template if template == "__default__" else template
        if tpl:
            body["template"] = {"name": tpl}
        r = self._request("POST", f"{API}/search/{self.search_id}", params={"company_id": self.company_id},
                          data=json.dumps(body))
        docs = (r.get("result") or {}).get("documents") or r.get("documents") or []
        if self.guard:
            self.guard.add_docs(len(docs))
        return docs

    def count(self, start, end):
        d = self._request("GET", f"{API}/searches/{self.search_id}/count",
                          params={"company_id": self.company_id, "start_date": iso(start, z=True),
                                  "end_date": iso(end, z=True)})
        return (d.get("count") or {}).get("total")

    # --- Export API (POST /v3/exports/one-time). Not search/analytics calls, so not counted by the daily guard. ---
    def create_export(self, start, end, count=None, template=None):
        """Queue a one-time export of this search for [start, end] (datetimes, UTC); returns the export id.
        `count` asks Meltwater for a random sample of at most that many docs (approximate, about +/-10%)."""
        body = {"search_ids": [str(self.search_id)], "start_date": iso(start, z=True), "end_date": iso(end, z=True)}
        if count:
            body["sample"] = {"count": int(count)}
        tpl = template or self.template
        if tpl:
            body["template"] = {"name": tpl}
        r = self._request("POST", f"{API}/exports/one-time", counted=False,
                          params={"company_id": self.company_id}, data=json.dumps({"onetime_export": body}))
        r = r.get("onetime_export", r)
        if not r.get("id"):
            raise MwError(f"Export was not created; unexpected response: {str(r)[:300]}")
        return str(r["id"])

    def export_status(self, export_id):
        """Returns (status, data_url). Status is PENDING until FINISHED."""
        r = self._request("GET", f"{API}/exports/one-time/{export_id}", counted=False,
                          params={"company_id": self.company_id})
        r = r.get("onetime_export", r)
        return str(r.get("status") or "").upper(), r.get("data_url")

    def download_export(self, data_url, path):
        """Stream a finished export to `path`. Plain request: the data URL is pre-signed, so the API key is NOT sent."""
        with requests.get(data_url, stream=True, timeout=300) as r:
            if r.status_code >= 400:
                raise MwError(f"Could not download the export (HTTP {r.status_code}).")
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)

    def create_search(self, name, boolean, case_sensitivity="no"):
        """Save a boolean search in this company (POST /v3/searches); returns (search_id, name).
        Names must be unique within the company."""
        body = {"search": {"name": name, "query": {"type": "boolean", "boolean": boolean,
                                                    "case_sensitivity": case_sensitivity}}}
        r = self._request("POST", f"{API}/searches", counted=False, params={"company_id": self.company_id},
                          data=json.dumps(body))
        r = r.get("search", r)
        if not r.get("id"):
            raise MwError(f"Search was not created; unexpected response: {str(r)[:300]}")
        return str(r["id"]), r.get("name", name)

    def usage(self, path, **params):
        """GET https://api.meltwater.com/v3/usage/me/<path> (authoritative usage numbers; not budgeted)."""
        return self._request("GET", f"{API}/usage/me/{path}", counted=False, params=params)

    def diagnose(self, start, end):
        """ONE count call, no retries, not budgeted: returns (status, limit-related headers, body snippet)."""
        r = self.s.request("GET", f"{API}/searches/{self.search_id}/count", timeout=60,
                           params={"company_id": self.company_id, "start_date": iso(start, z=True),
                                   "end_date": iso(end, z=True)})
        hdrs = {k: v for k, v in r.headers.items() if "limit" in k.lower() or "retry" in k.lower()}
        return r.status_code, hdrs, r.text[:300]
