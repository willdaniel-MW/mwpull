"""mwpull command line: pull | resume | probe | report | profile."""
import argparse
import getpass
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone

from . import __version__, config, profiles, update
from .limits import LIMIT_KEYS, LimitReached, UsageGuard, fmt_when, parse_headers
from .client import Client, MwError
from .output import COLUMNS, flatten, parse_fields, report
from .export import run_export
from .runner import finish, run
from .sampling import MODES

DESCRIPTION = "Pull or sample documents from a Meltwater saved search into CSV or JSONL (via the Export API by default)."
DEFAULT_TEMPLATE = "api.json"


def resolve_template(flag, prof):
    """--template wins, then the profile's, then api.json. 'none' sends no template."""
    t = flag or prof["template"] or DEFAULT_TEMPLATE
    return None if t.lower() == "none" else t


def yesterday():
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    m += 1
    for day in range(d.day, 27, -1):  # clamp e.g. Mar 31 - 1 month -> Feb 28/29
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, min(d.day, 28))


def resolve_range(a):
    end = date.fromisoformat(a.end) if a.end else yesterday()
    if a.start:
        if a.days or a.months:
            raise MwError("Use either --start, or --days/--months, not both")
        return date.fromisoformat(a.start), end
    if a.days:
        return end - timedelta(days=a.days - 1), end
    if a.months:
        return add_months(end, a.months) + timedelta(days=1), end
    raise MwError("Give a date range: --start YYYY-MM-DD [--end YYYY-MM-DD], or --days N, or --months N")


def add_pandas(p):
    p.add_argument("--pandas", action="store_true",
                   help="csv: plain UTF-8 with no byte-order mark. By default CSVs start with a BOM so Excel shows "
                        "non-English text correctly; scripts that read plain utf-8 would see it in the first "
                        "column name")


def add_boolean(p):
    p.add_argument("--boolean", nargs="?", const=ASK, metavar="QUERY",
                   help="no search ID yet? Save a boolean query as a new search and use it. Give the query in "
                        "single quotes, or leave the value off and mwpull asks you to paste it "
                        "(see `mwpull search create`)")
    p.add_argument("--search-name", help="name for the new search (default: mwpull <timestamp>)")


ASK = "\0ask"  # --boolean given with no value: prompt for it


def ask_boolean(boolean):
    if boolean != ASK:
        return boolean
    if not sys.stdin.isatty():
        raise MwError("--boolean needs the query as its value when input isn't a terminal")
    q = input("Boolean query (paste it as one line, no extra quotes needed): ").strip()
    if not q:
        raise MwError("No boolean query entered")
    return q


def ensure_search(a):
    """--boolean: save the query as a new search and carry on with its ID."""
    if not a.boolean:
        return
    if a.search_id:
        raise MwError("Use either --search-id or --boolean, not both")
    if a.resume:
        raise MwError("--resume needs --search-id (the search was already created; its ID was printed)")
    if a.dry_run:
        a.search_id = "new-search"
        print("Dry run: the search would be saved as a new search when you run for real.")
    else:
        prof = profiles.resolve(a.profile)
        a.search_id = create_search(prof, a.search_name, ask_boolean(a.boolean))
    a.boolean = None


def create_search(prof, name, boolean, case_sensitivity="no"):
    name = name or f"mwpull {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}"
    sid, name = Client(prof["api_key"], "-", prof["company_id"], delay=0).create_search(name, boolean, case_sensitivity)
    print(f"Saved search '{name}' with ID {sid}. Reuse it with --search-id {sid}.")
    return sid


def cmd_search(a):
    prof = profiles.resolve(a.profile)
    boolean = a.boolean
    if boolean is None:
        boolean = ASK
        if a.name is None and sys.stdin.isatty():
            a.name = input("Search name (Enter for an automatic name): ").strip() or None
    print(create_search(prof, a.name, ask_boolean(boolean), a.case_sensitivity))


def add_pull_args(p):
    g = p.add_argument_group("account and search")
    g.add_argument("--profile", help="account profile name (see `mwpull profile`); default profile if omitted")
    g.add_argument("--search-id", help="saved search ID (or env MELTWATER_SEARCH_ID)")
    add_boolean(g)
    g.add_argument("--languages", help="comma-separated language codes, or 'any' for no filter "
                                       "(default: profile's, else 'en')")
    g.add_argument("--template", help=f"search template to request (default: profile's, else {DEFAULT_TEMPLATE}; 'none' sends no template)")
    g = p.add_argument_group("date range (UTC, whole days; end date inclusive)")
    g.add_argument("--start", help="first day, YYYY-MM-DD")
    g.add_argument("--end", help="last day, YYYY-MM-DD (default: yesterday)")
    g.add_argument("--days", type=int, help="last N days ending at --end")
    g.add_argument("--months", type=int, help="last N months ending at --end")
    g = p.add_argument_group("sampling")
    g.add_argument("--mode", choices=MODES, default="all",
                   help="all (every doc; asks to confirm if large) | per-day (equal per day) | proportional "
                        "(per-day quota follows each day's volume) | random (random polls across the range). "
                        "The sampling modes are approximate by design and use few API calls. Default: all")
    g.add_argument("--max-docs", type=int, help="total documents wanted (omit for unbounded; not valid for 'all')")
    g.add_argument("--per-day", type=int, help="per-day mode: N docs from every day instead of a total")
    g.add_argument("--fraction", type=float, help="proportional/random: take this fraction of all docs, e.g. 0.1")
    g.add_argument("--chunk", type=int,
                   help="docs taken per API call when sampling, 1-100. Default: automatic, starting at 10 and "
                        "raised only if needed to fit the calls left on your daily budget. Bigger = fewer calls "
                        "but docs come in tighter time clusters")
    g.add_argument("--seed", type=int, help="random seed; recorded in the manifest (default: random)")
    g = p.add_argument_group("output")
    g.add_argument("--format", choices=("csv", "jsonl"), help="output format (default: your saved setting, else csv)")
    g.add_argument("--fields", help="comma-separated columns; add extras as name=dotted.path "
                                    f"(built-in: {','.join(COLUMNS)})")
    g.add_argument("--raw", action="store_true", help="jsonl only: include the full raw API document as 'raw'")
    g.add_argument("--out-dir", help="parent folder for run folders (default: $MWPULL_OUT_DIR, else your saved "
                                      "setting, else ./out; see `mwpull config`)")
    g.add_argument("--name", help="run folder name (default <search>_<start>_<end>_<mode>)")
    g.add_argument("--no-report", action="store_true", help="skip the QA report at the end")
    add_pandas(g)
    g = p.add_argument_group("how to fetch")
    g.add_argument("--via", choices=("export", "search"), default="export",
                   help="export (default): Meltwater's Export API, few calls, random samples (+/-10%%). "
                        "search: the search API, exact per-day quotas but 100 docs per call and a daily call budget")
    g.add_argument("--split", choices=("month", "week", "day", "none"),
                   help="export: one export per month/week/day, or a single one (default: from --mode)")
    g.add_argument("--per-slice", type=int, help="export: sample size for every slice (e.g. 5000 per month)")
    g.add_argument("--allow-long-window", action="store_true",
                   help="export: skip the 12-month-per-export check (only if your contract allows longer)")
    g.add_argument("--poll-interval", type=int, default=20, help="export: seconds between status checks")
    g.add_argument("--max-wait", type=int, default=180, help="export: minutes to wait for each export")
    g.add_argument("--keep-raw", action="store_true", help="export: keep the downloaded export files in raw/")
    g = p.add_argument_group("run control")
    g.add_argument("--dry-run", action="store_true", help="count and print the plan; pull nothing, write nothing")
    g.add_argument("--resume", action="store_true", help="continue the run folder with this name (same settings)")
    g.add_argument("--force", action="store_true", help="overwrite an existing run folder")
    g.add_argument("--wait", action="store_true",
                   help="if a usage limit with a known reset stops the run, sleep until the reset and continue")
    g.add_argument("--yes", action="store_true", help="--mode all: skip the confirmation for large pulls")
    g.add_argument("--confirm-over", type=int, default=50000,
                   help="--mode all: ask for confirmation above this many documents (default 50000)")
    g.add_argument("--delay", type=float, default=0.2, help="seconds between API calls (default 0.2)")


def build_args(a):
    """Validate CLI args into the dict the runner uses."""
    prof = profiles.resolve(a.profile)
    search_id = a.search_id or os.environ.get("MELTWATER_SEARCH_ID")
    if not search_id:
        raise MwError("Missing --search-id (or env MELTWATER_SEARCH_ID)")
    start, end = resolve_range(a)
    fmt = a.format or config.format_default()
    out_dir = a.out_dir or config.out_dir_default()
    if a.raw and fmt != "jsonl":
        raise MwError("--raw only works with --format jsonl")
    if a.chunk is not None and not 1 <= a.chunk <= 100:
        raise MwError("--chunk must be between 1 and 100")
    if a.fraction is not None and not 0 < a.fraction <= 1:
        raise MwError("--fraction must be between 0 and 1")
    if a.fraction is not None and a.max_docs is not None:
        raise MwError("Use --max-docs or --fraction, not both")
    if a.mode == "all" and a.fraction is not None:
        raise MwError("--mode all pulls everything; --max-docs / --per-day / --fraction don't apply")
    if a.mode == "per-day" and a.fraction is not None:
        raise MwError("--fraction applies to proportional/random; per-day takes --per-day N or --max-docs N")
    parse_fields(a.fields)  # fail early on a bad spec
    if a.languages:
        langs = [] if a.languages.strip().lower() == "any" else [x.strip() for x in a.languages.split(",") if x.strip()]
    else:
        langs = prof["languages"] if prof["languages"] is not None else ["en"]
    args = {
        "profile": prof["name"], "search_id": search_id, "company_id": prof["company_id"],
        "start": start.isoformat(), "end": end.isoformat(), "start_date": start, "end_date": end,
        "mode": a.mode, "max_docs": a.max_docs, "per_day": a.per_day, "fraction": a.fraction,
        "chunk": a.chunk or 10, "chunk_auto": a.chunk is None,
        "yes": a.yes, "confirm_over": a.confirm_over,
        "seed": a.seed if a.seed is not None else random.SystemRandom().randrange(2 ** 31),
        "languages": langs, "template": resolve_template(a.template, prof),
        "format": fmt, "fields": a.fields, "raw": a.raw,
        "out_dir": out_dir, "name": a.name or f"{search_id[:8]}_{start}_{end}_{a.mode}",
        "no_report": a.no_report, "dry_run": a.dry_run, "resume": a.resume, "force": a.force,
    }
    return args, prof


def adopt_seed(args):
    """On --resume with no --seed given, reuse the original run's seed."""
    mf = os.path.join(args["out_dir"], args["name"], "manifest.json")
    if args["resume"] and os.path.exists(mf):
        args["seed"] = json.load(open(mf))["args"].get("seed", args["seed"])


def make_client(prof, args, delay):
    return Client(prof["api_key"], args["search_id"], prof["company_id"], template=args["template"],
                  languages=args["languages"], delay=delay, guard=UsageGuard(prof["key"], prof["limits"]))


def execute(args, make, wait=False, sleep=time.sleep, now=time.time):
    """Run a pull. If a usage limit with a known reset stops it and wait=True, sleep until the reset, then resume."""
    while True:
        try:
            return run(make(), args)
        except LimitReached as e:
            if not wait or not e.reset:
                raise
            secs = max(0, e.reset - now()) + 30
            print(f"\n{e}\nWaiting for the reset, then continuing automatically (Ctrl-C to stop; "
                  f"progress is saved)...", file=sys.stderr)
            sleep(secs)
            if os.path.exists(os.path.join(args["out_dir"], args["name"], "manifest.json")):
                args = dict(args, resume=True, dry_run=False, force=False)


SPLIT_FOR_MODE = {"all": "month", "proportional": "month", "per-day": "day", "random": "none"}


def pull_via_export(a):
    """`pull` default: map the search-style options onto the Export API (see export.py)."""
    profiles.resolve(a.profile)  # fail first if no account is selected
    if a.fraction is not None:
        raise MwError("--fraction is a search-API option; use --max-docs N or --per-slice N for exports "
                      "(or --via search)")
    if a.raw and (a.format or config.format_default()) != "jsonl":
        raise MwError("--raw only works with --format jsonl")
    if a.mode == "all" and not (a.max_docs or a.per_slice or a.per_day) and not (a.yes or a.dry_run):
        if not sys.stdin.isatty():
            raise MwError("This exports every doc in the range and counts against your monthly document allowance. "
                          "Add --max-docs N / --per-slice N to sample, or --yes to export everything.")
        if input("Export EVERY doc in the range (uses your monthly document allowance)? [y/N] ").strip().lower() \
                not in ("y", "yes"):
            raise MwError("cancelled")
    per_slice = a.per_slice or a.per_day
    split = a.split or SPLIT_FOR_MODE[a.mode]
    ns = argparse.Namespace(**{**vars(a), "split": split, "per_slice": per_slice,
                               "everything": a.mode == "all" and not (a.max_docs or per_slice)})
    cmd_export(ns)


def cmd_pull(a):
    ensure_search(a)
    if a.via == "export":
        return pull_via_export(a)
    args, prof = build_args(a)
    if a.resume and a.seed is None:
        adopt_seed(args)
    if not args["dry_run"]:
        print(f"Output folder: {os.path.abspath(os.path.join(args['out_dir'], args['name']))}")
    path = execute(args, lambda: make_client(prof, args, a.delay), a.wait)
    finish(path, args)
    if path:
        print(f"\nDone. Data: {path}")


def cmd_export(a):
    ensure_search(a)
    prof = profiles.resolve(a.profile)
    search_id = a.search_id or os.environ.get("MELTWATER_SEARCH_ID")
    if not search_id:
        raise MwError("Missing --search-id (or env MELTWATER_SEARCH_ID)")
    if not (a.max_docs or a.per_slice or a.everything):
        raise MwError("Say how much to export: --max-docs N (total sample), --per-slice N, or --everything. "
                      "(Exports use your monthly document allowance, so there is no default.)")
    if sum(bool(x) for x in (a.max_docs, a.per_slice, a.everything)) > 1:
        raise MwError("Use only one of --max-docs, --per-slice, --everything")
    start, end = resolve_range(a)
    fmt = a.format or config.format_default()
    if a.raw and fmt != "jsonl":
        raise MwError("--raw only works with --format jsonl")
    parse_fields(a.fields)
    if a.languages not in (None, "any") and not a.dry_run:
        print("note: exports have no language filter; --languages is ignored (use --via search to filter)",
              file=sys.stderr)
    args = {"version": __version__, "profile": prof["name"], "search_id": search_id, "company_id": prof["company_id"],
            "start": start.isoformat(), "end": end.isoformat(), "start_date": start, "end_date": end,
            "split": a.split, "max_docs": a.max_docs, "per_slice": a.per_slice,
            "template": resolve_template(a.template, prof), "format": fmt, "fields": a.fields, "raw": a.raw,
            "out_dir": a.out_dir or config.out_dir_default(), "name": a.name or f"{search_id[:8]}_{start}_{end}_export",
            "no_report": a.no_report, "dry_run": a.dry_run, "resume": a.resume, "force": a.force,
            "pandas": a.pandas, "allow_long_window": a.allow_long_window, "poll_interval": a.poll_interval,
            "max_wait": a.max_wait * 60, "keep_raw": a.keep_raw}
    if not a.dry_run:
        print(f"Output folder: {os.path.abspath(os.path.join(args['out_dir'], args['name']))}")
    client = Client(prof["api_key"], search_id, prof["company_id"], template=args["template"], delay=a.delay)
    run_export(client, args)


def cmd_resume(a):
    mf = os.path.join(a.run_dir, "manifest.json")
    if not os.path.exists(mf):
        raise MwError(f"No manifest.json in {a.run_dir}")
    old = json.load(open(mf))["args"]
    prof = profiles.resolve(a.profile or old.get("profile"))
    args = dict(old)
    args.update(start_date=date.fromisoformat(old["start"]), end_date=date.fromisoformat(old["end"]),
                out_dir=os.path.dirname(os.path.abspath(a.run_dir)), name=os.path.basename(os.path.abspath(a.run_dir)),
                dry_run=False, resume=True, force=False)
    path = execute(args, lambda: make_client(prof, args, a.delay), a.wait)
    finish(path, args)
    print(f"\nDone. Data: {path}")


def preflight(client, prof, search_id, now=None):
    """Run the checks in order; returns [(ok, label, detail)] where ok is True / False / None (info only)."""
    now = now or datetime.now(timezone.utc)
    out = []
    who = prof["name"] or "(environment variables)"
    out.append((True, "Profile", f"{who}: company_id={prof['company_id']}, key ****{prof['api_key'][-4:]}, "
                                 f"template={client.template or '(none)'}, languages={client.languages or 'any'}"))
    start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(microsecond=0)
    try:
        total = client.count(start, end)
    except MwError as e:
        out.append((False, "Account + search access", str(e)))
        return out
    out.append((True, "Account + search access", f"search {search_id} reachable with this key and company ID"))
    if not total:
        out.append((False, "Search returns results",
                    "0 documents in the last 30 days. Check the search ID, the --languages filter, and that "
                    "the saved search has recent matches."))
        return out
    out.append((True, "Search returns results",
                f"{total} documents in the last 30 days (~{total / 30:.0f}/day)"))
    try:
        docs = client.search(start, end, 1, page_size=3, order="desc")
    except MwError as e:
        out.append((False, "Documents can be fetched", str(e)))
        return out
    if not docs:
        out.append((False, "Documents can be fetched", "count > 0 but the search call returned no documents; "
                                                        "try --template none, or a different --template"))
        return out
    rows = [flatten(d, "") for d in docs]
    out.append((True, "Documents can be fetched", f"fetched {len(rows)}; newest: {rows[0]['published_date']} "
                                                  f"{rows[0]['domain'] or '(no domain)'} | "
                                                  f"{(rows[0]['title'] or rows[0]['full_text'] or '')[:60]!r}"))
    g = getattr(client, "guard", None)
    if g:
        st = g.status()
        srv = (f"; Meltwater reports {st['day_remaining_server']} of {st['day_limit_server']} daily calls left"
               if st["day_limit_server"] is not None else "")
        reset = f", day window resets {fmt_when(st['day_reset'])}" if st["day_reset"] else ""
        out.append((None, "Usage limits", f"pacing at {st['calls_per_minute']} calls/min; "
                                          f"{st['day_calls']} calls in the current day window{reset}{srv}"))
    have = sum(bool(r["full_text"]) for r in rows)
    out.append((None, "Full text", f"{have}/{len(rows)} sample docs include full_text "
                                   "(this is controlled by your API key's entitlements, not a setting here)"))
    return out


def cmd_probe(a):
    prof = profiles.resolve(a.profile)
    search_id = a.search_id or os.environ.get("MELTWATER_SEARCH_ID")
    if not search_id:
        raise MwError("Missing --search-id (or env MELTWATER_SEARCH_ID)")
    langs = [] if (a.languages or "").lower() == "any" else [x for x in (a.languages or "en").split(",") if x]
    c = Client(prof["api_key"], search_id, prof["company_id"], template=resolve_template(a.template, prof),
               languages=langs)
    results = preflight(c, prof, search_id)
    for ok, label, detail in results:
        print(f"  {'OK  ' if ok else 'FAIL' if ok is False else 'info'}  {label}: {detail}")
    if any(ok is False for ok, _, _ in results):
        sys.exit("\nPreflight FAILED - fix the item above before a bigger pull.")
    print("\nPreflight passed. Next: mwpull pull ... --dry-run to preview the plan.")


def cmd_check_update(a):
    latest = update.latest_version(force=True)
    if latest is None:
        print(f"mwpull {__version__}. Couldn't check for updates (offline, or no git access to the repo).")
    elif update.is_newer(latest, __version__):
        print(f"mwpull {latest} is available (you have {__version__}).\nUpdate: {update.upgrade_command(latest)}")
    else:
        print(f"mwpull {__version__} is up to date (latest release: {latest}).")


def cmd_config(a):
    cfg = config.load()
    if a.action == "path":
        print(config.config_path())
    elif a.action == "list":
        for k, desc in config.KEYS.items():
            print(f"{k:8} = {cfg.get(k, '(not set)')}    # {desc}")
        if os.environ.get("MWPULL_OUT_DIR"):
            print(f"\nMWPULL_OUT_DIR={os.environ['MWPULL_OUT_DIR']} is set and overrides out_dir")
    elif a.action == "get":
        if a.key not in config.KEYS:
            raise MwError(f"Unknown setting '{a.key}'. Available: {', '.join(config.KEYS)}")
        print(cfg.get(a.key, "(not set)"))
    elif a.action == "set":
        if not a.key or a.value is None:
            raise MwError("Usage: mwpull config set KEY VALUE   (e.g. mwpull config set out_dir ~/Documents/pulls)")
        cfg[a.key] = config.normalize(a.key, a.value)
        config.save(cfg)
        print(f"{a.key} = {cfg[a.key]}")
    elif a.action == "unset":
        if a.key not in config.KEYS:
            raise MwError(f"Unknown setting '{a.key}'. Available: {', '.join(config.KEYS)}")
        cfg.pop(a.key, None)
        config.save(cfg)
        print(f"{a.key} cleared (back to the built-in default)")


def cmd_report(a):
    fmt = a.format or ("jsonl" if a.file.endswith(".jsonl") else "csv")
    report(a.file, fmt)


def limits_from_args(a, current):
    """Apply --calls-per-minute/--calls-per-day/--docs-per-month to a limits dict; 0 clears a limit."""
    lim = dict(current)
    for k in LIMIT_KEYS:
        v = getattr(a, k)
        if v is None:
            continue
        if v < 0:
            raise MwError(f"--{k.replace('_', '-')} can't be negative")
        if v == 0:
            lim.pop(k, None)
        else:
            lim[k] = v
    return lim


def print_capacity(st):
    """Plain-language summary of what a key can do right now."""
    print("\nWhat this key can do:")
    print(f"  per minute : {st['minute_limit'] or 'unknown'} calls")
    if st["hour_limit"]:
        print(f"  per hour   : {st['hour_limit']} calls ({st['hour_remaining']} left)")
    day = st["calls_per_day"]
    if day:
        left = st["calls_left_today"]
        print(f"  per day    : {day} calls ({left} left"
              + (f", resets {fmt_when(st['day_reset'])}" if st["day_reset"] else "") + ")")
        print(f"  => today's remaining budget pulls up to ~{left * 10} docs at the default 10 per call, or "
              f"~{left * 100} at --chunk 100 (each call returns at most 100 documents).")
    else:
        print("  per day    : unknown (no daily limit reported)")


def diagnose(prof, search_id):
    c = Client(prof["api_key"], search_id, prof["company_id"], delay=0)
    end = datetime.now(timezone.utc).replace(microsecond=0)
    status, hdrs, body = c.diagnose(end - timedelta(days=1), end)
    g = UsageGuard(prof["key"], prof["limits"])
    g.after_call(hdrs)  # remember the limits/block so later runs plan around them
    print(f"\nDiagnostic call (search {search_id}, last 24h count, no retries): HTTP {status}")
    print("Rate-limit related headers:" + ("" if hdrs else " (none returned)"))
    for k, v in hdrs.items():
        print(f"  {k}: {v}")
    info = parse_headers(hdrs)
    for scope in ("day", "hour", "minute"):
        if info[f"{scope}_reset"]:
            print(f"  -> {scope} window resets {fmt_when(info[f'{scope}_reset'])}")
    print(f"Body: {body}")
    print_capacity(g.status())
    if status == 429 and not hdrs:
        print("\nMeltwater refused the call without saying why or when. Typical causes: the per-minute cap "
              "(clears within a minute or two), the per-IP hourly cap of 2,000 calls (clears when the hour "
              "rolls over), or a daily cap. Wait a few minutes and run this again: if it succeeds the block was "
              "short-lived. If it still fails after an hour, ask your Meltwater account manager.")


def cmd_usage(a):
    prof = profiles.resolve(a.profile)
    if a.diagnose or a.check:
        sid = a.search_id or os.environ.get("MELTWATER_SEARCH_ID")
        if not sid:
            raise MwError("--check/--diagnose needs --search-id (or env MELTWATER_SEARCH_ID)")
        diagnose(prof, sid)
        return
    g = UsageGuard(prof["key"], prof["limits"])
    s = g.status()
    who = prof["name"] or "(environment variables)"
    print(f"Profile: {who}   (limits are shared by every key in company {prof['company_id']})")
    print("Limits mwpull enforces:")
    print(f"  calls per minute : {s['calls_per_minute']}"
          + ("" if prof["limits"].get("calls_per_minute") else "  (built-in default; documented search limit is 100/min)"))
    print(f"  calls per day    : {s['calls_per_day'] or 'not set'}")
    print(f"  documents/month  : {s['docs_per_month'] or 'not set'}")
    print("Tracked by mwpull on this machine:")
    reset = f", window resets {fmt_when(s['day_reset'])}" if s["day_reset"] else ""
    print(f"  calls in current day window: {s['day_calls']}{reset}")
    print(f"  documents fetched this month: {s['month_docs']}")
    if s["day_limit_server"] is not None:
        print(f"Meltwater last reported: {s['day_remaining_server']} of {s['day_limit_server']} daily calls remaining")
    if s["blocked_until"]:
        print(f"\nBLOCKED: {s['blocked_reason']}. Resets {fmt_when(s['blocked_until'])}.")
    elif g.knows_limits():
        print("\nNot blocked, according to the last responses mwpull saw.")
    else:
        print("\nUNKNOWN: mwpull has never seen this company's limits on this machine, so it can't say whether "
              "it's blocked.\nFor a definitive answer (costs 1 call): mwpull usage --profile "
              f"{prof['name'] or '<name>'} --search-id <ID> --check")
    if a.remote:
        c = Client(prof["api_key"], "-", prof["company_id"], delay=0)
        print("\nMeltwater's own numbers:")
        for label, path, params in (("API requests, last 24 hours", "requests", {"period": "24hours"}),
                                    ("API requests, last 30 days", "requests", {"period": "30days"}),
                                    ("search documents, last 30 days", "metrics/document_count",
                                     {"feature": "search", "period": "30days"})):
            try:
                print(f"  {label}: {c.usage(path, **params).get('count')}")
            except MwError as e:
                print(f"  {label}: unavailable ({e})")
        print("(Meltwater's daily window starts at your first request after it resets, so 24h counts are an "
              "approximation of the daily limit.)")
    else:
        print("Add --remote to also ask Meltwater for its authoritative usage counts.")


def cmd_profile(a):
    data = profiles.load()
    if a.action == "path":
        print(profiles.profiles_path())
    elif a.action == "list":
        for n, p in data["profiles"].items():
            mark = "*" if n == data["default"] else " "
            print(f"{mark} {n}  company_id={p.get('company_id')}  template={p.get('template')}  "
                  f"languages={p.get('languages')}  api_key=****{str(p.get('api_key', ''))[-4:]}  "
                  f"limits={p.get('limits') or '(defaults)'}")
        if not data["profiles"]:
            print("No profiles yet. Add one: mwpull profile add NAME")
    elif a.action == "add":
        if not a.name:
            raise MwError("Usage: mwpull profile add NAME")
        key = getpass.getpass("API key (input hidden): ").strip()
        cid = input("Company ID: ").strip()
        if not key or not cid:
            raise MwError("API key and company ID are both required")
        entry = {"api_key": key, "company_id": cid, "template": a.template,
                 "languages": [x for x in a.languages.split(",") if x] if a.languages else None}
        lim = limits_from_args(a, {})
        if lim:
            entry["limits"] = lim
        data["profiles"][a.name] = entry
        if a.set_default or not data["default"]:
            data["default"] = a.name
        profiles.save(data)
        print(f"Saved profile '{a.name}' to {profiles.profiles_path()} (default: {data['default']})")
    elif a.action == "remove":
        if a.name not in data["profiles"]:
            raise MwError(f"No profile '{a.name}'")
        del data["profiles"][a.name]
        if data["default"] == a.name:
            data["default"] = next(iter(data["profiles"]), None)
        profiles.save(data)
        print(f"Removed '{a.name}'")
    elif a.action == "limits":
        if a.name not in data["profiles"]:
            raise MwError(f"No profile '{a.name}'. Usage: mwpull profile limits NAME --calls-per-day N ...")
        prof = data["profiles"][a.name]
        lim = limits_from_args(a, prof.get("limits") or {})
        if lim == (prof.get("limits") or {}) and not any(getattr(a, k) is not None for k in LIMIT_KEYS):
            raise MwError("Give at least one of --calls-per-minute / --calls-per-day / --docs-per-month "
                          "(use 0 to clear a limit)")
        prof["limits"] = lim
        profiles.save(data)
        print(f"{a.name}: limits = {lim or '(defaults)'}")
    elif a.action == "default":
        if a.name not in data["profiles"]:
            raise MwError(f"No profile '{a.name}'")
        data["default"] = a.name
        profiles.save(data)
        print(f"Default profile is now '{a.name}'")


def parser():
    ap = argparse.ArgumentParser(prog="mwpull", description=DESCRIPTION)
    ap.add_argument("--version", action="version", version=f"mwpull {__version__}")
    ap.add_argument("--no-update-check", action="store_true",
                    help="skip the once-a-day check for a newer release (or set MWPULL_NO_UPDATE_CHECK=1)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pull", help="pull or sample documents", description=DESCRIPTION)
    add_pull_args(p)
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("export", help="sample or pull via the Export API (few calls; the right tool for big pulls)",
                       description="Create one export per date slice (a random sample each), wait, download, and "
                                   "combine into one CSV/JSONL. Uses export quotas, not search calls.")
    p.add_argument("--profile")
    p.add_argument("--search-id", help="saved search ID (or env MELTWATER_SEARCH_ID)")
    add_boolean(p)
    p.add_argument("--languages", help=argparse.SUPPRESS)
    p.add_argument("--start", help="first day, YYYY-MM-DD")
    p.add_argument("--end", help="last day, YYYY-MM-DD (default: yesterday)")
    p.add_argument("--days", type=int)
    p.add_argument("--months", type=int)
    p.add_argument("--split", choices=("month", "week", "day", "none"), default="month",
                   help="one export per month (default), week or day; 'none' = a single export")
    p.add_argument("--max-docs", type=int, help="total sample, divided across slices by days")
    p.add_argument("--per-slice", type=int, help="sample size for every slice (e.g. 5000 per month)")
    p.add_argument("--everything", action="store_true", help="no sample: every doc (up to 2,000,000 per export)")
    p.add_argument("--template", help=f"output template (default: profile's, else {DEFAULT_TEMPLATE}; must be JSON)")
    p.add_argument("--format", choices=("csv", "jsonl"))
    p.add_argument("--fields", help=f"comma-separated columns; extras as name=dotted.path (built-in: {','.join(COLUMNS)})")
    p.add_argument("--raw", action="store_true", help="jsonl only: include the full raw document")
    add_pandas(p)
    p.add_argument("--out-dir")
    p.add_argument("--name")
    p.add_argument("--no-report", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="print the slices and sample sizes; create nothing")
    p.add_argument("--resume", action="store_true", help="continue the run folder with this name")
    p.add_argument("--force", action="store_true")
    p.add_argument("--poll-interval", type=int, default=20, help="seconds between status checks (default 20)")
    p.add_argument("--max-wait", type=int, default=180, help="minutes to wait for each export (default 180)")
    p.add_argument("--keep-raw", action="store_true", help="keep the downloaded export files in raw/")
    p.add_argument("--allow-long-window", action="store_true",
                   help="skip the 12-month-per-export check (only if your contract allows longer)")
    p.add_argument("--delay", type=float, default=0.2)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("search", help="save a boolean query as a new Meltwater search, to get a search ID")
    p.add_argument("action", choices=("create",))
    p.add_argument("--profile")
    p.add_argument("--name", help="search name (must be unique in the account; default: mwpull <timestamp>)")
    p.add_argument("--boolean", help="the boolean query, in single quotes. Leave it off and mwpull asks you to "
                                    "paste it (no quoting needed)")
    p.add_argument("--case-sensitivity", choices=("no", "yes", "hybrid"), default="no")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("resume", help="continue an interrupted run from its folder (reads its manifest)")
    p.add_argument("run_dir")
    p.add_argument("--profile", help="override the profile recorded in the manifest")
    p.add_argument("--delay", type=float, default=0.2)
    p.add_argument("--wait", action="store_true", help="on a usage limit, sleep until the reset and continue")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("probe", help="preflight check: profile, search access, results, a sample fetch")
    p.add_argument("--profile")
    p.add_argument("--search-id")
    p.add_argument("--languages")
    p.add_argument("--template")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("usage", help="show a profile's limits, what mwpull has used, and when a block resets")
    p.add_argument("--profile")
    p.add_argument("--remote", action="store_true", help="also fetch Meltwater's own usage counts")
    p.add_argument("--check", action="store_true",
                   help="make ONE search-count call and show what this key can do (limits and what's left), "
                        "remembered for planning; same as --diagnose")
    p.add_argument("--diagnose", action="store_true",
                   help="make ONE search-count call (no retries) and print Meltwater's exact response and "
                        "rate-limit headers; use with --search-id when you're getting 429s")
    p.add_argument("--search-id", help="for --diagnose (or env MELTWATER_SEARCH_ID)")
    p.set_defaults(fn=cmd_usage)

    p = sub.add_parser("check-update", help="check now whether a newer mwpull release exists")
    p.set_defaults(fn=cmd_check_update)

    p = sub.add_parser("config", help="saved defaults, e.g. where run folders go (~/.meltwater/config.json)")
    p.add_argument("action", choices=("list", "get", "set", "unset", "path"))
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("report", help="QA report for an existing output file")
    p.add_argument("file")
    p.add_argument("--format", choices=("csv", "jsonl"))
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("profile", help="manage account profiles (~/.meltwater/profiles.json)")
    p.add_argument("action", choices=("add", "list", "remove", "default", "limits", "path"))
    p.add_argument("name", nargs="?")
    p.add_argument("--calls-per-minute", dest="calls_per_minute", type=int,
                   help="add/limits: your pace ceiling (default 90; search is documented at 100/min)")
    p.add_argument("--calls-per-day", dest="calls_per_day", type=int,
                   help="add/limits: daily API-call budget for this profile (e.g. 50 or 100); 0 clears")
    p.add_argument("--docs-per-month", dest="docs_per_month", type=int,
                   help="add/limits: monthly document budget (e.g. 30000); 0 clears")
    p.add_argument("--template", help="add: default template for this profile")
    p.add_argument("--languages", help="add: default languages, comma-separated")
    p.add_argument("--set-default", action="store_true", help="add: make this the default profile")
    p.set_defaults(fn=cmd_profile)
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    if a.cmd in ("pull", "resume", "probe", "export") and not a.no_update_check:
        try:
            msg = update.notice()
            if msg:
                print(msg, file=sys.stderr)
        except Exception:
            pass  # an update check must never break a run
    try:
        a.fn(a)
    except LimitReached as e:
        msg = f"stopped: {e}"
        if e.run_dir:
            msg += (f"\nProgress is saved. Continue after the reset with:  mwpull resume \"{e.run_dir}\"\n"
                    f"(or add --wait to sleep until the reset and continue automatically)")
        print(msg, file=sys.stderr)
        sys.exit(3)
    except MwError as e:
        sys.exit(f"error: {e}")
    except ValueError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("\ninterrupted - progress is checkpointed; continue with `mwpull resume <run folder>`")


if __name__ == "__main__":
    main()
