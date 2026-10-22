"""Document -> row mapping, CSV/JSONL writers, and the post-run QA report."""
import csv
import json
import os
from collections import Counter

COLUMNS = ["document_id", "title", "published_date", "language", "url", "domain", "source_name",
           "source_type", "content_type", "snippet", "full_text", "week_start"]


def get(d, *path):
    for p in path:
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
    }


def parse_fields(spec):
    """'a,b,sent=enrichments.sentiment' -> [(name, None | ['enrichments','sentiment'])]. None spec = defaults."""
    if not spec:
        return [(c, None) for c in COLUMNS]
    out = []
    for item in (x.strip() for x in spec.split(",") if x.strip()):
        if "=" in item:
            name, path = item.split("=", 1)
            out.append((name.strip(), path.strip().split(".")))
        elif item in COLUMNS:
            out.append((item, None))
        else:
            raise ValueError(f"Unknown field '{item}'. Built-in: {', '.join(COLUMNS)}. "
                             f"For raw API values use name=dotted.path (e.g. sentiment=enrichments.sentiment).")
    return out


def build_row(doc, week_start, fields):
    base = flatten(doc, week_start)
    row = {}
    for name, path in fields:
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


def report(path, fmt, target=None):
    rows = list(read_rows(path, fmt))
    n = len(rows)
    tgt = f" (target {target})" if target else ""
    print(f"\n===== QA report: {path} =====")
    print(f"rows: {n}{tgt}; unique ids: {len({r.get('document_id') for r in rows})}")
    if not n:
        return
    cols = set(rows[0])
    if "full_text" in cols:
        print(f"empty full_text: {sum(not r['full_text'] for r in rows) / n:.1%}")
    if "language" in cols:
        print("languages:", dict(Counter(r["language"] for r in rows).most_common(5)))
    if "published_date" in cols:
        months = Counter((r["published_date"] or "")[:7] for r in rows)
        days = Counter((r["published_date"] or "")[:10] for r in rows)
        print("docs per month:", dict(sorted(months.items())))
        print(f"docs per day: min={min(days.values())} max={max(days.values())} days={len(days)}")
    for col in ("source_type", "content_type", "source_name", "domain"):
        if col not in cols:
            continue
        top = Counter(r[col] or "(blank)" for r in rows).most_common(20 if col in ("source_name", "domain") else 10)
        print(f"\ntop {col}:")
        for k, v in top:
            print(f"  {v / n:6.1%}  {v:6d}  {k}")
