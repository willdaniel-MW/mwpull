"""Plan a pull, fetch it day by day, and write it with crash-safe checkpoints.

Run directory layout (<out_dir>/<name>/):
  documents.csv|jsonl   the data
  manifest.json         every argument used (never the API key) - also the resume fingerprint
  plan.json             per-day quota (and count, where counted), frozen at plan time
  checkpoint.json       done days + byte offset of the data file after the last completed day
"""
import json
import math
import os
import random
import sys
from datetime import timedelta

from . import __version__
from .client import MwError, PAGE_SIZE
from .fetch import poll_day, scan_all
from .limits import LimitReached
from .output import RowWriter, build_row, parse_fields, report
from .sampling import day_windows, make_quotas

METHOD = 2  # bump when sampling/plan semantics change; older run folders can't be resumed
# Manifest keys that must match for --resume to be allowed.
FINGERPRINT = ("search_id", "company_id", "start", "end", "mode", "max_docs", "per_day", "fraction", "chunk",
               "seed", "languages", "template", "format", "fields", "raw")


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def monday(d0):
    return (d0 - timedelta(days=d0.weekday())).strftime("%Y-%m-%d")


def ask_confirm(msg):
    if not sys.stdin.isatty():
        raise MwError(msg + "\nRe-run with --yes to proceed non-interactively.")
    return input(msg + "\nProceed? [y/N] ").strip().lower() in ("y", "yes")


def plan_pull(client, a, rng, log=print):
    """Returns (plan [{day,count,quota}], total docs in range or None). Counting is kept to the minimum:
    `all` makes one count call (for the confirmation), `proportional` one per day (that is the curve),
    `per-day` / `random` none (unless --fraction needs the total: one call)."""
    wins = day_windows(a["start_date"], a["end_date"])
    nd, mode = len(wins), a["mode"]
    guard = getattr(client, "guard", None)
    if guard and not guard.knows_limits():  # one cheap call teaches us this key's real limits (from the headers)
        log("Checking this key's call limits (1 call)...")
        client.count(wins[0][1], wins[0][2])
    counts, total = [None] * nd, None
    if mode == "proportional":
        log(f"Counting {nd} days to match the volume curve...")
        counts = [client.count(s, e) or 0 for _, s, e in wins]
        total = sum(counts)
    elif mode == "all" or a.get("fraction") is not None:
        total = client.count(wins[0][1], wins[-1][2]) or 0
    max_docs = a["max_docs"]
    if a.get("fraction") is not None:
        max_docs = round(total * a["fraction"])
    def build(c):
        return make_quotas(mode, nd, counts, max_docs, a.get("per_day"), c, random.Random(a["seed"]))

    quotas = build(a["chunk"])
    left = guard.status()["calls_left_today"] if guard else None
    if a.get("chunk_auto") and mode != "all" and left is not None:
        def need(qs, c):
            return sum(math.ceil(q / c) for q in qs if q)
        if need(quotas, a["chunk"]) > left:  # raise docs-per-call until the sample fits today's remaining calls
            for c in range(a["chunk"] + 1, 101):
                if need(build(c), c) <= left:
                    break
            old, a["chunk"] = a["chunk"], c
            quotas = build(c)
            fits = need(quotas, c) <= left
            log(f"Auto-adjusted --chunk from {old} to {c} " + (f"so the sample fits the {left} calls left on today's "
                f"budget." if fits else f"(the maximum); it still needs ~{need(quotas, c)} calls but only {left} "
                f"are left today. Pass --chunk N to choose it yourself."))
    return [{"day": d, "count": c, "quota": q} for (d, _, _), c, q in zip(wins, counts, quotas)], total


def estimate_calls(plan, a, total):
    if a["mode"] == "all":
        return math.ceil((total or 0) / PAGE_SIZE) + len(plan)
    extra = len(plan) if a["mode"] == "proportional" else 0
    return sum(math.ceil(p["quota"] / a["chunk"]) for p in plan if p["quota"]) + extra


def print_plan(plan, a, total):
    calls = estimate_calls(plan, a, total)
    print(f"\nmode={a['mode']}  range={a['start']}..{a['end']}  days={len(plan)}")
    if a["mode"] == "all":
        print(f"documents in range: {total}   to pull: all of them")
    else:
        want = sum(p["quota"] for p in plan)
        active = [p for p in plan if p["quota"]]
        print(f"to pull: ~{want} docs across {len(active)} days"
              + (f" (of {total} in range)" if total is not None else ""))
        if active:
            qs = [p["quota"] for p in active]
            print(f"per-day quota: min={min(qs)} max={max(qs)} mean={want / len(active):.1f}   "
                  f"({a['chunk']} docs per API call)")
    print(f"estimated API calls: ~{calls}  (about {calls * 0.5 / 60:.0f} min at ~2 calls/sec)")


def warn_budget(client, plan, a, total):
    """Heads-up when the planned run won't fit this profile's remaining call/document budgets."""
    guard = getattr(client, "guard", None)
    if not guard:
        return
    calls = estimate_calls(plan, a, total)
    docs = (total or 0) if a["mode"] == "all" else sum(p["quota"] for p in plan)
    notes = guard.warnings(calls, docs)
    if notes and a["mode"] != "all" and a["chunk"] < 100:
        fewer = estimate_calls(plan, dict(a, chunk=100), total)
        if fewer < calls:
            notes.append(f"with --chunk 100 the same sample needs ~{fewer} call{'s' if fewer != 1 else ''} "
                         f"(instead of ~{calls}).")
    for n in notes:
        print(f"HEADS-UP: {n}")


def run(client, a, log=print, confirm=ask_confirm):
    """a: argument dict (see cli.build_args). Returns the data file path (or None for dry runs)."""
    run_dir = os.path.join(a["out_dir"], a["name"])
    paths = {k: os.path.join(run_dir, v) for k, v in
             {"data": f"documents.{a['format']}", "manifest": "manifest.json", "plan": "plan.json",
              "ck": "checkpoint.json"}.items()}
    fields = parse_fields(a["fields"])
    wins = {d: (s, e) for d, s, e in day_windows(a["start_date"], a["end_date"])}
    rng = random.Random(a["seed"])

    resuming = a["resume"]
    if resuming:
        if not os.path.exists(paths["manifest"]):
            raise MwError(f"Nothing to resume at {run_dir}")
        mf = json.load(open(paths["manifest"]))
        if mf.get("method") != METHOD:
            raise MwError("This run folder was made by an older mwpull sampling method and can't be resumed. "
                          "Start a new run (add --force to overwrite it).")
        diff = [k for k in FINGERPRINT if mf["args"].get(k) != a.get(k)]
        if diff:
            raise MwError("Can't resume: these settings differ from the original run: " + ", ".join(diff))
        plan = json.load(open(paths["plan"]))
        ck = json.load(open(paths["ck"]))
    else:
        plan, total = plan_pull(client, a, rng, log)
        print_plan(plan, a, total)
        warn_budget(client, plan, a, total)
        if a["dry_run"]:
            return None
        if a["mode"] == "all" and total > a["confirm_over"] and not a["yes"]:
            if not confirm(f"\nThis will pull ALL {total} documents (~{estimate_calls(plan, a, total)} API calls)."):
                raise MwError("Cancelled; nothing was pulled.")
        if os.path.exists(run_dir) and os.listdir(run_dir) and not a["force"]:
            raise MwError(f"{run_dir} already exists. Use --resume to continue it, --force to overwrite, "
                          f"or pick another --name.")
        os.makedirs(run_dir, exist_ok=True)
        skip = ("start_date", "end_date", "dry_run", "resume", "force", "yes", "confirm_over")
        write_json(paths["manifest"], {"mwpull": __version__, "method": METHOD,
                                       "args": {k: v for k, v in a.items() if k not in skip}})
        write_json(paths["plan"], plan)
        ck = {"done_days": [], "offset": None, "written": 0}

    if resuming:
        if os.path.getsize(paths["data"]) < ck["offset"]:
            raise MwError("Data file is shorter than the checkpoint says; can't resume safely.")
        with open(paths["data"], "r+b") as f:  # drop any rows written after the last completed day
            f.truncate(ck["offset"])
        writer = RowWriter(paths["data"], a["format"], fields, a["raw"], append=True, bom=not a.get("pandas"))
        log(f"Resuming: {len(ck['done_days'])} days done, {ck['written']} docs written")
    else:
        writer = RowWriter(paths["data"], a["format"], fields, a["raw"], bom=not a.get("pandas"))
        writer.flush()
        ck["offset"] = os.path.getsize(paths["data"])
        write_json(paths["ck"], ck)

    done, written = set(ck["done_days"]), ck["written"]
    todo = [p for p in plan if p["quota"] != 0]  # quota None means "all"
    try:
        for n, p in enumerate(todo, 1):
            day = p["day"]
            if day in done:
                continue
            ds, de = wins[day]
            ws = monday(ds)
            if p["quota"] is None:
                docs = scan_all(client, ds, de, log)
            else:
                docs = poll_day(client, ds, de, p["quota"], a["chunk"], a["seed"], day)
            got = 0
            for doc in docs:
                writer.write(build_row(doc, ws, fields), doc)
                got += 1
            writer.flush()
            written += got
            done.add(day)
            write_json(paths["ck"], {"done_days": sorted(done), "offset": os.path.getsize(paths["data"]),
                                     "written": written})
            want = "all" if p["quota"] is None else p["quota"]
            log(f"[{n}/{len(todo)}] {day}: +{got} (wanted {want}) total={written}")
    except LimitReached as e:
        e.run_dir = run_dir  # lets the CLI print the exact resume command
        raise
    finally:
        writer.close()
    return paths["data"]


def finish(path, a):
    if path and not a["no_report"]:
        want = None if a["mode"] == "all" else a.get("max_docs")
        report(path, a["format"], want)
