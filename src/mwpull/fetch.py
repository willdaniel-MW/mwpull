"""Fetch strategies. Both avoid counting/sizing calls.

scan_all  pull every document in a window, in time order. The API returns at most 1,000 docs per query,
          so after a full 10 pages we restart from the last document's timestamp (duplicates dropped).
poll_day  take ~`quota` docs from a day with a few small calls: each call asks for `chunk` consecutive
          docs starting at (or ending at) a random moment in the day. Approximate by design.
"""
import math
import random
from datetime import datetime, timedelta, timezone

from .client import MAX_PAGES, MwError, PAGE_SIZE
from .output import first

ONE_SEC = timedelta(seconds=1)


def doc_id(doc):
    return doc.get("id") or doc.get("document_id")


def doc_time(doc):
    v = first(doc.get("published_date"), doc.get("date"))
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)


def scan_all(client, start, end, log=print):
    """Yield every doc in [start, end] exactly once, oldest first."""
    cursor, seen = start, set()
    while cursor <= end:
        got = []
        for page in range(1, MAX_PAGES + 1):
            batch = client.search(cursor, end, page, PAGE_SIZE, "asc")
            got += batch
            if len(batch) < PAGE_SIZE:
                break
        for d in got:
            i = doc_id(d)
            if i and i not in seen:
                seen.add(i)
                yield d
        if len(got) < PAGE_SIZE * MAX_PAGES:
            return  # window exhausted
        last = doc_time(got[-1])
        if last is None:
            raise MwError("Can't page past 1,000 documents: results have no usable published date.")
        if last <= cursor:  # 1,000+ docs share one second; step past it rather than loop forever
            log(f"  warning: 1,000+ documents at {cursor:%Y-%m-%d %H:%M:%S}; some in that second are skipped")
            last = cursor + ONE_SEC
        cursor = last  # inclusive restart: docs in that second are re-fetched, then de-duplicated


def poll_day(client, start, end, quota, chunk, seed, day):
    """Return up to `quota` docs from the day via small random polls (deterministic for a given seed)."""
    rng = random.Random(f"{seed}:{day}")
    span = int((end - start).total_seconds())
    docs, seen = [], set()
    attempts, max_attempts = 0, 3 * math.ceil(quota / chunk) + 3
    while len(docs) < quota and attempts < max_attempts:
        attempts += 1
        want = min(chunk, quota - len(docs))
        t = start + timedelta(seconds=rng.randint(0, span))
        if rng.random() < 0.5:
            batch = client.search(t, end, 1, want, "asc")    # `want` docs from t onward
        else:
            batch = client.search(start, t, 1, want, "desc")  # `want` docs ending at t
        for d in batch:
            i = doc_id(d)
            if i and i not in seen and len(docs) < quota:
                seen.add(i)
                docs.append(d)
    return docs
