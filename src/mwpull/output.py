"""Document -> row mapping, CSV/JSONL writers, and the post-run QA report."""
import csv
import json
import os
from collections import Counter

COLUMNS = ["document_id", "title", "published_date", "language", "url", "domain", "source_name",
           "source_type", "content_type", "snippet", "full_text", "week_start"]


# Every field Meltwater's api.json template returns, as dotted paths (verified on a 12,213-document pull).
# Lists (keyphrases, named_entities, matched.*, ...) are written as JSON text in one cell.
API_PATHS = [
    "author.external_id", "author.handle", "author.name", "author.profile_url",
    "content.byline", "content.emojis", "content.hashtags", "content.image", "content.links", "content.mentions",
    "custom.custom_categories", "custom.custom_fields", "custom.hidden", "custom.tags", "custom.visible",
    "enrichments.keyphrases", "enrichments.named_entities",
    "external_id", "indexed_date",
    "location.city", "location.country_code", "location.geo.latitude", "location.geo.longitude",
    "location.region", "location.state",
    "matched.hit_sentence", "matched.inputs", "matched.keywords",
    "metrics.earned_media_value", "metrics.editorial_echo",
    "metrics.engagement.comments", "metrics.engagement.likes", "metrics.engagement.quotes",
    "metrics.engagement.reactions", "metrics.engagement.replies", "metrics.engagement.reposts",
    "metrics.engagement.shares", "metrics.engagement.total",
    "metrics.episode_reach", "metrics.estimated_views",
    "metrics.social_echo.bluesky", "metrics.social_echo.facebook", "metrics.social_echo.reddit",
    "metrics.social_echo.total", "metrics.social_echo.x", "metrics.views",
    "parent.url", "source.id", "source.information_type",
    "source.metrics.ave", "source.metrics.local_reach", "source.metrics.national_reach",
    "source.metrics.national_viewership", "source.metrics.reach", "source.metrics.reach_desktop",
    "source.metrics.reach_mobile", "source.outlet_types", "source.url",
    "thread.title", "thread.url",
]
# Paths already covered by a friendly column in COLUMNS (or the sentiment column), so "all" doesn't repeat them.
OTHER = "other_fields"  # JSON of any field Meltwater adds that isn't in API_PATHS yet, so nothing is silently dropped
_KNOWN = set(API_PATHS) | {"id", "document_id", "published_date", "date", "url", "content_type", "content.title",
                           "content.body", "content.opening_text", "enrichments.language_code",
                           "enrichments.sentiment", "source.domain", "source.name", "source.type"}


def get(d, *path):
    """Walk a dotted path. A path may run through a list of objects (matched.inputs.name): the values found in
    each item are joined with '; '."""
    for i, p in enumerate(path):
        if isinstance(d, list):
            vals = [v for v in (get(x, *path[i:]) for x in d) if v not in (None, "")]
            return "; ".join(str(v) for v in vals) if vals else None
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def first(*vals):
    for v in vals:
        if v not in (None, "", []):
            return v
    return ""


def flatten(doc, week_start):
    """Map a document (nested or flat template shape) to the default row."""
    return {
        "document_id": first(doc.get("id"), doc.get("document_id")),
        "title": first(get(doc, "content", "title"), doc.get("title")),
        "published_date": first(doc.get("published_date"), doc.get("date")),
        "language": first(get(doc, "enrichments", "language_code"), doc.get("language"), doc.get("language_code")),
        "url": first(doc.get("url")),
        "domain": first(get(doc, "source", "domain"), doc.get("domain")),
        "source_name": first(get(doc, "source", "name"), doc.get("source_name")),
        "source_type": first(get(doc, "source", "type"), get(doc, "source", "information_type"),
                             doc.get("source_type")),
        "content_type": first(doc.get("content_type")),
        "snippet": first(get(doc, "content", "opening_text"), doc.get("opening_text"), doc.get("snippet")),
        "full_text": first(get(doc, "content", "body"), doc.get("body"),
                           doc.get("content") if isinstance(doc.get("content"), str) else None),
        "week_start": week_start,
        "sentiment": first(get(doc, "enrichments", "sentiment"), doc.get("sentiment")),
    }


def _leaves(o, prefix=()):
    if isinstance(o, dict) and o:
        for k, v in o.items():
            yield from _leaves(v, prefix + (k,))
    else:
        yield ".".join(prefix), o


def other_fields(doc):
    """Fields in this document that mwpull doesn't have a column for (empty string if none)."""
    extra = {k: v for k, v in _leaves(doc) if k and k not in _KNOWN and not k.startswith("location.geo")}
    return json.dumps(extra, ensure_ascii=False) if extra else ""


PRESETS = {
    "all": COLUMNS[:-1] + ["sentiment", "week_start"] + [p for p in API_PATHS] + [OTHER],
    "basic": list(COLUMNS),
}
PRESET_HELP = {"all": "every field Meltwater returns, plus sentiment and a week_start column (the default)",
               "basic": "document_id, title, published_date, language, url, domain, source_name, source_type, "
                        "content_type, snippet, full_text, week_start"}


def parse_fields(spec):
    """'all' / 'basic' / 'a,b,sent=enrichments.sentiment' -> [(name, None | ['enrichments','sentiment'])].
    Presets (all, basic) can be mixed with other items: 'basic,sentiment,reach=source.metrics.reach'.
    Empty spec = all."""
    items = [x.strip() for x in (spec or "all").split(",") if x.strip()] or ["all"]
    out, seen = [], set()

    def add(name, path):
        if name not in seen:
            seen.add(name)
            out.append((name, path))

    for item in items:
        if item in PRESETS:
            for c in PRESETS[item]:
                add(c, None if (c in COLUMNS or c in ("sentiment", OTHER)) else c.split("."))
        elif "=" in item:
            name, path = item.split("=", 1)
            add(name.strip(), path.strip().split("."))
        elif item in COLUMNS or item == "sentiment":
            add(item, None)
        else:
            raise ValueError(f"Unknown field '{item}'. Presets: {', '.join(PRESETS)}. Built-in columns: "
                             f"{', '.join(COLUMNS)}, sentiment. For raw API values use name=dotted.path "
                             f"(e.g. reach=source.metrics.reach).")
    return out


def build_row(doc, week_start, fields):
    base = flatten(doc, week_start)
    row = {}
    for name, path in fields:
        if name == OTHER and path is None:
            row[name] = other_fields(doc)
        else:
            row[name] = base[name] if path is None else get(doc, *path)
    return row


class RowWriter:
    """Streams rows to CSV or JSONL. Open with append=True to continue a resumed run."""

    def __init__(self, path, fmt, fields, raw=False, append=False, bom=True):
        self.fmt, self.fields, self.raw = fmt, [n for n, _ in fields], raw
        # bom=True (default): a UTF-8 byte-order mark so Excel opens non-English text correctly. Scripts that read
        # CSVs as plain utf-8 would see it in the first column name; --pandas turns it off.
        enc = "utf-8-sig" if bom and fmt == "csv" and not append else "utf-8"
        self.f = open(path, "a" if append else "w", newline="", encoding=enc)
        self.w = None
        if fmt == "csv":
            self.w = csv.DictWriter(self.f, fieldnames=self.fields, quoting=csv.QUOTE_ALL)
            if not append:
                self.w.writeheader()
        self.f.flush()

    def write(self, row, doc=None):
        if self.fmt == "csv":
            self.w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else ("" if v is None else v))
                             for k, v in row.items()})
        else:
            rec = dict(row)
            if self.raw:
                rec["raw"] = doc
            self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def flush(self):
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self):
        self.f.close()


def read_rows(path, fmt):
    with open(path, encoding="utf-8-sig", newline="") as f:  # -sig: accept files with or without the BOM
        if fmt == "csv":
            yield from csv.DictReader(f)
        else:
            for line in f:
                if line.strip():
                    yield json.loads(line)


# Where each QA-report column comes from in the raw API document, so the report still works when a field
# profile renames columns (e.g. "Document ID=id").
REPORT_PATHS = {"document_id": "id", "full_text": "content.body", "language": "enrichments.language_code",
                "published_date": "published_date", "source_type": "source.type", "content_type": "content_type",
                "source_name": "source.name", "domain": "source.domain"}


def report_columns(cols, fields):
    """Map report keys to the actual column names in the file (None if the file has no such column)."""
    renamed = {}
    for name, path in parse_fields(fields) if fields else []:
        renamed[".".join(path) if path else name] = name
    return {k: k if k in cols else renamed.get(v) if renamed.get(v) in cols else None
            for k, v in REPORT_PATHS.items()}


def report(path, fmt, target=None, fields=None):
    rows = list(read_rows(path, fmt))
    n = len(rows)
    tgt = f" (target {target})" if target else ""
    print(f"\n===== QA report: {path} =====")
    c = report_columns(set(rows[0]) if rows else set(), fields)
    ids = f"; unique ids: {len({r.get(c['document_id']) for r in rows})}" if c["document_id"] else ""
    print(f"rows: {n}{tgt}{ids}")
    if not n:
        return
    if c["full_text"]:
        print(f"empty full_text: {sum(not r[c['full_text']] for r in rows) / n:.1%}")
    if c["language"]:
        print("languages:", dict(Counter(r[c["language"]] for r in rows).most_common(5)))
    if c["published_date"]:
        pd = c["published_date"]
        months = Counter((r[pd] or "")[:7] for r in rows)
        days = Counter((r[pd] or "")[:10] for r in rows)
        print("docs per month:", dict(sorted(months.items())))
        print(f"docs per day: min={min(days.values())} max={max(days.values())} days={len(days)}")
    for key in ("source_type", "content_type", "source_name", "domain"):
        col = c[key]
        if not col:
            continue
        top = Counter(r[col] or "(blank)" for r in rows).most_common(20 if key in ("source_name", "domain") else 10)
        print(f"\ntop {key}:")
        for k, v in top:
            print(f"  {v / n:6.1%}  {v:6d}  {k}")
