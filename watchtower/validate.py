"""Decide whether a read is correct (the 'Notice' part). Deterministic, no LLM."""
from __future__ import annotations

import re
from datetime import datetime

from .normalize import is_iso_date, looks_like_date_or_number

PROFILE_FIELDS = ["title", "url", "buyerName", "publicationDate", "submissionDeadline",
                  "referenceNumber", "description", "cpvCodes", "documents"]


def profile(tenders: list[dict]) -> dict:
    """Fill rate per field (0..1) plus count — the read's fingerprint."""
    n = len(tenders) or 1
    prof = {"count": len(tenders)}
    for f in PROFILE_FIELDS:
        prof[f] = round(sum(1 for t in tenders if t.get(f)) / n, 3)
    titles = [t.get("title") or "" for t in tenders]
    prof["avgTitleLen"] = round(sum(len(x) for x in titles) / n, 1)
    return prof


def check(tenders: list[dict], good_profile: dict | None, good_count: int,
          first_read: bool) -> tuple[bool, list[str], list[str]]:
    """Return (ok, problems, warnings)."""
    problems: list[str] = []
    warnings: list[str] = []
    n = len(tenders)
    if n == 0:
        problems.append("No tenders extracted from the tender list")
        return False, problems, warnings
    ids = [t.get("id") for t in tenders]
    if any(not i for i in ids):
        problems.append(f"{sum(1 for i in ids if not i)} tenders without id")
    if len(set(ids)) != len(ids):
        problems.append("Duplicate tender ids")
    for f in ("title", "url"):
        miss = sum(1 for t in tenders if not t.get(f))
        if miss:
            problems.append(f"{miss}/{n} tenders without {f}")
    bad_dates = sum(1 for t in tenders if t.get("publicationDate")
                    and not is_iso_date(t["publicationDate"]))
    if bad_dates:
        problems.append(f"{bad_dates} malformed publication dates")
    inverted = 0
    for t in tenders:
        pd, dl = t.get("publicationDate"), t.get("submissionDeadline")
        if pd and dl:
            try:
                if datetime.fromisoformat(dl).date().isoformat() < pd:
                    inverted += 1
            except ValueError:
                inverted += 1
    if inverted > max(1, n * 0.2):
        problems.append(f"{inverted}/{n} deadlines before publication date (fields swapped?)")
    numeric_titles = sum(1 for t in tenders if looks_like_date_or_number(t.get("title")))
    if numeric_titles > n * 0.3:
        problems.append(f"{numeric_titles}/{n} titles look like dates/numbers (wrong column?)")
    if any(t.get("cpvCodes") and not all(re.fullmatch(r"\d{8}", c) for c in t["cpvCodes"])
           for t in tenders):
        problems.append("Malformed CPV codes")
    if first_read:
        if sum(1 for t in tenders if t.get("submissionDeadline")) == 0:
            warnings.append("No submission deadlines found")
        if sum(1 for t in tenders if t.get("buyerName")) == 0:
            warnings.append("No buyer names found")
    if good_profile and not first_read:
        cur = profile(tenders)
        for f in PROFILE_FIELDS:
            before, now = good_profile.get(f, 0), cur.get(f, 0)
            if before >= 0.5 and now < before - 0.35:
                problems.append(f"Field '{f}' fill rate dropped {before:.0%} → {now:.0%}")
        if good_count >= 5 and n < good_count * 0.4:
            problems.append(f"Tender count dropped sharply: {good_count} → {n}")
        lb, la = good_profile.get("avgTitleLen", 0), cur["avgTitleLen"]
        if lb > 15 and (la < lb * 0.35 or la > lb * 3.5):
            problems.append(f"Title length changed {lb:.0f} → {la:.0f} chars (wrong column?)")
    return not problems, problems, warnings
