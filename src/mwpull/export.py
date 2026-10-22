"""Pull a saved search through Meltwater's Export API: one export per date slice, each a random sample.

Why: the search API returns at most 100 docs per call and 1,000 per query, so big pulls burn the daily call
budget. An export returns thousands of docs per request (up to 2,000,000) without using search calls.

Run directory layout (<out_dir>/<name>/):
  documents.csv|jsonl   the data
  manifest.json         every argument used (never the API key)
  export_state.json     per-slice export id, status and the data file's byte offset after each finished slice
  raw/                  downloaded export files (deleted after conversion unless --keep-raw)
"""
import json
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from .client import MwError
from .output import RowWriter, build_row, parse_fields, read_rows, report
from .runner import write_json

CREATE_PAUSE = 3.5  # exports are limited to 20 creates/minute
FAILED = ("FAILED", "ERROR", "CANCELLED", "CANCELED", "EXPIRED")


def slices(start, end, split):
    """[(label, first_day, last_day)] covering start..end inclusive, cut at month / week / day boundaries."""
    out, d = [], start
    while d <= end:
        if split == "month":
            nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
            label = d.strftime("%Y-%m")
        elif split == "week":
            nxt = d + timedelta(days=7 - d.weekday())
            label = d.strftime("%Y-%m-%d")
        elif split == "day":
            nxt, label = d + timedelta(days=1), d.strftime("%Y-%m-%d")
        else:
            nxt, label = end + timedelta(days=1), f"{start}_{end}"
        last = min(end, nxt - timedelta(days=1))
        out.append((label, d, last))
        d = last + timedelta(days=1)
    return out


def plan_slices(start, end, split, max_docs=None, per_slice=None):
    """Sample size per slice: --per-slice N exactly, or --max-docs split across slices by number of days
    (so every day carries about the same weight). Neither -> None (the whole slice, up to 2,000,000 docs)."""
    sl = slices(start, end, split)
    total_days = (end - start).days + 1
    plan = []
    for label, s, e in sl:
        days = (e - s).days + 1
        count = per_slice if per_slice else (max(1, round(max_docs * days / total_days)) if max_docs else None)
        plan.append({"label": label, "start": s.isoformat(), "end": e.isoformat(), "days": days, "count": count,
                     "export_id": None, "status": None, "done": False, "offset": 0, "rows": 0})
    return plan


def too_long(start, end):
    """True if [start, end] (inclusive days) spans more than 12 months, the longest window one export may cover
    unless your contract says otherwise."""
    try:
        limit = date(start.year + 1, start.month, start.day)
    except ValueError:  # Feb 29
        limit = date(start.year + 1, 3, 1)
    return end >= limit


def _dt(day, end=False):
    d = date.fromisoformat(day)
    return datetime.combine(d, dtime(23, 59, 59) if end else dtime(0, 0, 0), tzinfo=timezone.utc)


def extract_docs(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    docs = data if isinstance(data, list) else (data.get("docs") or data.get("documents") or [])
    if not isinstance(docs, list):
        raise MwError(f"{path}: expected a JSON export with a 'docs' list (use the api.json template).")
    return docs


def week_start(doc):
    try:
        d = date.fromisoformat((doc.get("published_date") or doc.get("date") or "")[:10])
        return (d - timedelta(days=d.weekday())).isoformat()
    except ValueError:
        return ""


def run_export(client, a, sleep=time.sleep, log=print):
    """Create (or resume) the exports, wait for each, download, convert and de-duplicate into one file."""
    run_dir = os.path.join(a["out_dir"], a["name"])
    mf, st_path = os.path.join(run_dir, "manifest.json"), os.path.join(run_dir, "export_state.json")
    plan = plan_slices(a["start_date"], a["end_date"], a["split"], a.get("max_docs"), a.get("per_slice"))
    if not a.get("allow_long_window"):
        for p in plan:
            if too_long(date.fromisoformat(p["start"]), date.fromisoformat(p["end"])):
                raise MwError(f"One export can cover at most 12 months, but {p['start']}..{p['end']} is longer. "
                              f"Use --split month (or week/day), or --allow-long-window if your contract "
                              f"allows longer exports.")
    want = sum(p["count"] or 0 for p in plan)
    if len(plan) > 50:
        log(f"warning: {len(plan)} exports; standard plans allow about 50 exports a month (check your contract). "
            f"Use a coarser --split.")
    log(f"{len(plan)} export(s) by {a['split']}; sample total {want if want else 'everything (no sample)'}"
        + (f" docs (approximate, about +/-10% per export)" if want else ""))
    if a["dry_run"]:
        for p in plan:
            log(f"  {p['label']}: {p['start']}..{p['end']}  sample={p['count'] or 'all'}")
        log("Dry run: nothing created.")
        return None
    if os.path.exists(run_dir) and not (a["resume"] or a["force"]):
        raise MwError(f"{run_dir} already exists. Use --resume to continue it or --force to overwrite.")
    os.makedirs(os.path.join(run_dir, "raw"), exist_ok=True)
    if a["resume"] and os.path.exists(st_path):
        old = json.load(open(st_path))["slices"]
        if [(p["label"], p["start"], p["end"], p["count"]) for p in old] != \
           [(p["label"], p["start"], p["end"], p["count"]) for p in plan]:
            raise MwError("This run folder was made with different dates/sampling; start a new --name or use --force.")
        plan = old
    else:
        write_json(mf, {"mwpull": a["version"], "method": "export",
                        "args": {k: v for k, v in a.items() if not k.endswith("_date")}})

    def save():
        write_json(st_path, {"slices": plan})

    save()
    # 1) Queue every export first, saving each id immediately: a crash/resume never creates a duplicate export.
    for p in plan:
        if p["export_id"]:
            continue
        p["export_id"] = client.create_export(_dt(p["start"]), _dt(p["end"], True), p["count"], a["template"])
        p["status"] = "PENDING"
        save()
        log(f"  queued {p['label']} (export {p['export_id']}, sample={p['count'] or 'all'})")
        sleep(CREATE_PAUSE)

    # 2) Collect them in order.
    path = os.path.join(run_dir, f"documents.{a['format']}")
    fields = parse_fields(a["fields"])
    seen, append = set(), False
    if a["resume"] and os.path.exists(path) and any(p["done"] for p in plan):
        done_off = max(p["offset"] for p in plan if p["done"])
        with open(path, "r+b") as f:
            f.truncate(done_off)
        seen = {r.get("document_id") for r in read_rows(path, a["format"])}
        append = True
    w = RowWriter(path, a["format"], fields, raw=a["raw"], append=append, bom=not a.get("pandas"))
    try:
        for p in plan:
            if p["done"]:
                continue
            waited = 0
            while True:
                status, url = client.export_status(p["export_id"])
                if status == "FINISHED" and url:
                    break
                if status in FAILED:
                    raise MwError(f"Export {p['export_id']} ({p['label']}) ended as {status}.")
                if waited > a["max_wait"]:
                    raise MwError(f"Export {p['export_id']} ({p['label']}) still {status or 'unknown'} after "
                                  f"{a['max_wait'] // 60} min. Continue later with --resume.")
                log(f"  {p['label']}: {status or 'waiting'}; checking again in {a['poll_interval']}s")
                sleep(a["poll_interval"])
                waited += a["poll_interval"]
            raw = os.path.join(run_dir, "raw", f"{p['label']}.json")
            client.download_export(url, raw)
            n = dup = 0
            for doc in extract_docs(raw):
                did = doc.get("id") or doc.get("document_id")
                if did in seen:
                    dup += 1
                    continue
                seen.add(did)
                w.write(build_row(doc, week_start(doc), fields), doc)
                n += 1
            w.flush()
            p.update(done=True, offset=os.path.getsize(path), rows=n, status="FINISHED")
            save()
            if not a["keep_raw"]:
                os.remove(raw)
            log(f"  {p['label']}: +{n} docs" + (f" ({dup} duplicates skipped)" if dup else ""))
    finally:
        w.close()
    log(f"\nDone. {sum(p['rows'] for p in plan)} docs. Data: {path}")
    if not a["no_report"]:
        report(path, a["format"], want or None)
    return path
