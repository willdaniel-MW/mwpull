"""Language and source filters, expressed as boolean operators appended to a search's query.

The API can't toggle Meltwater's UI filters, so they have to live in the boolean itself. This module is the one
place that knows the operators and how they combine, so every command builds them the same way.

Rules (from Meltwater's boolean operators):
  * language:"en"   - 2- or 3-letter language code. Several: language:("en" OR "fr").
  * infoType / socialType / broadcastType values may be grouped with OR *only inside one operator*:
        socialType:("twitter" OR "reddit")      ok
        infoType:"news" OR socialType:"twitter"   not supported here: refused, never guessed
  * No language / no source given = no filter (everything the search returns).
"""
import re

from .client import MwError

# friendly name -> (operator, value). Names are what users type after --source; the raw operator form
# (e.g. socialType:"twitter") is accepted too.
SOURCES = {
    "news": ("infoType", "news"),
    "social": ("infoType", "social"),
    "broadcast": ("infoType", "broadcast"),
    "twitter": ("socialType", "twitter"),
    "x": ("socialType", "twitter"),
    "facebook": ("socialType", "facebook"),
    "instagram": ("socialType", "instagram"),
    "reddit": ("socialType", "reddit"),
    "youtube": ("socialType", "youtube"),
    "kakaotalk": ("socialType", "kakaotalk"),
    "linevoom": ("socialType", "linevoom"),
    "tiktok": ("socialType", "tiktok"),
    "red": ("socialType", "red"),
    "xiaohongshu": ("socialType", "red"),
    "sina_weibo": ("socialType", "sina_weibo"),
    "weibo": ("socialType", "sina_weibo"),
    "wechat": ("socialType", "wechat"),
    "line": ("socialType", "line"),
    "social_comments": ("socialType", "social_comments"),
    "social_reviews": ("socialType", "social_reviews"),
    "social_message_boards": ("socialType", "social_message_boards"),
    "social_blogs": ("socialType", "social_blogs"),
    "podcasts": ("socialType", "podcasts"),
    "radio": ("broadcastType", "au"),
    "audio": ("broadcastType", "au"),
    "tv": ("broadcastType", "vi"),
    "video": ("broadcastType", "vi"),
}
OPERATORS = ("infoType", "socialType", "broadcastType")
RAW = re.compile(r'^(infoType|socialType|broadcastType)\s*:\s*"?([A-Za-z_]+)"?$')
LANG = re.compile(r"^[a-z]{2,3}$")
ANY = ("", "any", "all")

SOURCE_HELP = ("news, social, broadcast, twitter, facebook, instagram, reddit, youtube, tiktok, wechat, "
               "sina_weibo, line, linevoom, kakaotalk, red, social_comments, social_reviews, "
               "social_message_boards, social_blogs, podcasts, radio, tv. Several is fine if they share one "
               "operator (e.g. twitter,reddit). Or give the raw form: 'infoType:\"news\"'")


def _split(spec):
    return [x.strip() for x in re.split(r"[,;]|\s+OR\s+", spec or "") if x.strip()]


def _group(op, values):
    vals = list(dict.fromkeys(values))  # de-duplicate, keep order
    if len(vals) == 1:
        return f'{op}:"{vals[0]}"'
    return f'{op}:(' + " OR ".join(f'"{v}"' for v in vals) + ")"


def language_clause(spec):
    """'en' -> language:"en"; 'en,fr' -> language:("en" OR "fr"); None/'any' -> None."""
    if spec is None or spec.strip().lower() in ANY:
        return None
    codes = [c.lower() for c in _split(spec)]
    bad = [c for c in codes if not LANG.match(c)]
    if bad or not codes:
        raise MwError(f"Language must be a 2- or 3-letter code such as en, fr, de, zh, fil (got: {spec!r}). "
                      "Leave --language off for all languages.")
    return _group("language", codes)


def source_clause(spec):
    """'news' -> infoType:"news"; 'twitter,reddit' -> socialType:("twitter" OR "reddit"); None/'any' -> None."""
    if spec is None or spec.strip().lower() in ANY:
        return None
    by_op = {}
    for tok in _split(spec):
        m = RAW.match(tok)
        if m:
            op, val = m.group(1), m.group(2)
        elif tok.lower() in SOURCES:
            op, val = SOURCES[tok.lower()]
        else:
            raise MwError(f"Unknown source {tok!r}. Choose from: {SOURCE_HELP}")
        by_op.setdefault(op, []).append(val)
    if not by_op:
        raise MwError("--source needs at least one value")
    if len(by_op) > 1:
        raise MwError("Sources that use different operators can't be combined with OR in one search "
                      f"({', '.join(sorted(by_op))}). Pull them separately, or use one operator, "
                      "e.g. --source twitter,reddit.")
    (op, vals), = by_op.items()
    return _group(op, vals)


def _is_single_term(boolean):
    return re.fullmatch(r'"[^"]*"', boolean) is not None or re.fullmatch(r"[^\s()\"]+", boolean) is not None


def apply_filters(boolean, language=None, source=None):
    """Return the boolean with the language/source filters ANDed on. The original is wrapped in parentheses
    (unless it is a single term) so an OR inside it can't escape the filter."""
    boolean = (boolean or "").strip()
    if not boolean:
        raise MwError("No boolean query to filter")
    clauses = []
    for clause, op_prefix in ((language_clause(language), "language:"), (source_clause(source), None)):
        if not clause:
            continue
        ops = [clause.split(":", 1)[0]]
        for op in ops:
            if re.search(rf"\b{op}\s*:", boolean, re.I):
                raise MwError(f"The boolean already contains a {op}: filter, so I won't add another. "
                              "Remove it from the boolean or drop the flag.")
        clauses.append(clause)
    if not clauses:
        return boolean
    base = boolean if _is_single_term(boolean) else f"({boolean})"
    return " AND ".join([base] + clauses)
