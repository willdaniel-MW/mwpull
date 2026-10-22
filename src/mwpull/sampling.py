"""Day windows and per-day quotas for each mode.

Sampling here is deliberately approximate and cheap. Quotas say how many documents to ask for on each
day; fetch.py then gets them with a handful of API calls (no per-day sizing, no exact positions).
  all           quota None  -> pull everything (fetch.scan_all)
  per-day       equal quota per day (no counting needed)
  proportional  quota follows each day's volume (one count call per day: that IS the curve)
  random        each poll lands on a uniformly random day/moment of the range (no counting needed)
"""
from datetime import datetime, timedelta, timezone

from .client import MwError

MODES = ("all", "per-day", "proportional", "random")
ONE_SEC = timedelta(seconds=1)


def day_windows(start, end):
    """Inclusive date range -> [(day 'YYYY-MM-DD', window_start, window_end)] (UTC, whole days)."""
    if end < start:
        raise MwError(f"--end ({end}) is before --start ({start})")
    out, d = [], start
    while d <= end:
        s = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        out.append((d.isoformat(), s, s + timedelta(days=1) - ONE_SEC))
        d += timedelta(days=1)
    return out


def largest_remainder(counts, total, rng):
    """Quota per day proportional to its volume; integer-exact (sums to `total`)."""
    s = sum(counts)
    if total >= s:
        return list(counts)
    exact = [total * c / s for c in counts]
    quotas = [int(x) for x in exact]
    order = sorted(range(len(counts)), key=lambda i: (-(exact[i] - quotas[i]), rng.random()))
    for i in order[: total - sum(quotas)]:
        quotas[i] += 1
    return quotas


def make_quotas(mode, n_days, counts, max_docs, per_day, chunk, rng):
    """Docs to take per day (list of ints), or [None]*n_days for `all`. `counts` only used by proportional."""
    if mode == "all":
        if max_docs is not None or per_day is not None:
            raise MwError("--mode all pulls everything; --max-docs / --per-day / --fraction don't apply")
        return [None] * n_days
    if mode == "per-day":
        if per_day is not None:
            return [per_day] * n_days
        if max_docs is None:
            raise MwError("--mode per-day needs --per-day N or --max-docs N")
        base, extra = divmod(max_docs, n_days)
        quotas = [base] * n_days
        for i in rng.sample(range(n_days), extra):
            quotas[i] += 1
        return quotas
    if max_docs is None:
        raise MwError(f"--mode {mode} needs --max-docs N or --fraction F")
    if mode == "proportional":
        return largest_remainder(counts, max_docs, rng)
    # random: each poll (up to `chunk` docs) lands on a uniformly random day
    quotas, left = [0] * n_days, max_docs
    while left > 0:
        take = min(chunk, left)
        quotas[rng.randrange(n_days)] += take
        left -= take
    return quotas
